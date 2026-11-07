"""Kubernetes Secrets client for the executor only.

It authenticates through the connector's write target (config.executor), as moks-executor, which holds Secret verbs
only in the namespaces listed under secretsSync.namespaces in the chart. The read-only connector used everywhere
else still cannot read Secrets.

The Kubernetes API has no call that returns key names without values, so a listing carries values through this
process. summary() keeps names, type, key names and ownership facts, and drops the values and every annotation:
kubectl's last-applied-configuration annotation holds a full copy of the data in clear text.

Updates send the resourceVersion the plan was approved against, so the API server refuses a Secret that changed
since (409). Deletes carry uid and resourceVersion preconditions.
"""
from __future__ import annotations

import asyncio
from typing import Any

from ..config import get_settings
from ..connectors.base import ConnectorError
from ..connectors.kubernetes import KubernetesConnector, _api_error
from .content import Content, from_kubernetes, to_kubernetes
from .ownership import AWS_VERSION_ANNOTATION, MAPPING_ANNOTATION, MOKS_LABEL, kubernetes_manager

LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"
PAGE = 250
MAX_PAGES = 40


def _last_modified(meta: dict[str, Any]) -> str | None:
    times = [m.get("time") for m in meta.get("managedFields") or [] if m.get("time")]
    return max(times) if times else meta.get("creationTimestamp")


def summary(obj: dict[str, Any]) -> dict[str, Any]:
    """What MOKS may keep about a Secret. Never values, never annotations."""
    meta = obj.get("metadata") or {}
    return {"name": meta.get("name"), "namespace": meta.get("namespace"), "uid": meta.get("uid"), "resource_version": meta.get("resourceVersion"),
            "type": obj.get("type") or "Opaque", "keys": sorted((obj.get("data") or {}).keys()), "immutable": bool(obj.get("immutable")),
            "last_modified": _last_modified(meta), "created": meta.get("creationTimestamp"), "last_applied": LAST_APPLIED in (meta.get("annotations") or {}),
            "owner": kubernetes_manager(meta, obj.get("type"))}


def consumers_of(name: str, workloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Workloads that read this Secret, and how. Environment variables are read once at container start, so they
    need a restart; mounted volumes are refreshed by the kubelet within about a minute unless mounted with subPath,
    and the application still has to reread the file; imagePullSecrets matter only for the next image pull."""
    out = []
    for w in workloads:
        spec = w.get("pod_spec") or {}
        how: set[str] = set()
        keys: set[str] = set()
        mounted_volumes = {v.get("name") for v in spec.get("volumes") or [] if (v.get("secret") or {}).get("secretName") == name}
        for v in spec.get("volumes") or []:
            for src in (v.get("projected") or {}).get("sources") or []:
                if (src.get("secret") or {}).get("name") == name:
                    mounted_volumes.add(v.get("name"))
        sub_path = False
        for c in (spec.get("containers") or []) + (spec.get("initContainers") or []):
            for e in c.get("env") or []:
                ref = ((e.get("valueFrom") or {}).get("secretKeyRef")) or {}
                if ref.get("name") == name:
                    how.add("env")
                    keys.add(ref.get("key"))
            for e in c.get("envFrom") or []:
                if (e.get("secretRef") or {}).get("name") == name:
                    how.add("envFrom")
            for m in c.get("volumeMounts") or []:
                if m.get("name") in mounted_volumes:
                    how.add("volume")
                    sub_path = sub_path or bool(m.get("subPath"))
        if any(p.get("name") == name for p in spec.get("imagePullSecrets") or []):
            how.add("imagePullSecrets")
        if not how:
            continue
        restart = bool(how & {"env", "envFrom"}) or sub_path
        note = ("environment variables are read at container start: restart to pick up the change" if how & {"env", "envFrom"} else
                "mounted with subPath, which is never refreshed: restart to pick up the change" if sub_path else
                "the mounted file updates within about a minute; the application must reread it" if "volume" in how else
                "used only when an image is next pulled")
        out.append({"kind": w["kind"], "name": w["name"], "how": sorted(how), "keys": sorted(k for k in keys if k), "restart_required": restart, "note": note,
                    "restartable": w["kind"] in ("Deployment",)})
    return out


class SecretsKube:
    def __init__(self, config: dict[str, Any], credential_ref: dict[str, Any] | None = None):
        if get_settings().process_role != "executor":
            raise ConnectorError("forbidden", "Kubernetes Secrets are read and written only by the executor process")
        target = (config or {}).get("executor")
        if not target:
            raise ConnectorError("config", "the kubernetes connector has no write target (config.executor)")
        self.read = KubernetesConnector(target, (config or {}).get("executor_credential_ref") or credential_ref)

    async def aclose(self) -> None:
        await self.read.aclose()

    async def _call(self, what: str, fn, *args, **kwargs):
        try:
            return await asyncio.wait_for(fn(*args, **kwargs), 30)
        except ConnectorError:
            raise
        except Exception as exc:
            if getattr(exc, "status", None) == 409:
                raise ConnectorError("conflict", f"{what}: the Secret changed since it was read (409 conflict)") from None
            if getattr(exc, "status", None) == 422:
                raise ConnectorError("bad_request", f"Kubernetes rejected {what} (422 invalid)") from None  # the body can echo the object: never pass it on
            raise _api_error(exc, what) from None

    async def _obj(self, value) -> dict[str, Any]:
        api = await self.read._client()
        return api.sanitize_for_serialization(value)

    async def cluster_uid(self) -> str:
        return await self.read.cluster_uid()

    async def list(self, namespace: str) -> tuple[list[dict[str, Any]], bool]:
        core = await self.read.core()
        out, token = [], None
        for _ in range(MAX_PAGES):
            kwargs: dict[str, Any] = {"limit": PAGE}
            if token:
                kwargs["_continue"] = token
            page = await self._call(f"list secrets in {namespace}", core.list_namespaced_secret, namespace, **kwargs)
            body = await self._obj(page)
            out += [summary(item) for item in body.get("items") or []]
            token = (body.get("metadata") or {}).get("continue")
            del body, page  # the values go no further than this loop
            if not token:
                return out, True
        return out, False

    async def get(self, namespace: str, name: str) -> tuple[dict[str, Any], Content] | None:
        core = await self.read.core()
        try:
            obj = await self._obj(await self._call(f"read secret {namespace}/{name}", core.read_namespaced_secret, name, namespace))
        except ConnectorError as exc:
            if exc.kind == "not_found":
                return None
            raise
        return summary(obj), from_kubernetes(obj.get("type"), obj.get("data"))

    async def create(self, namespace: str, name: str, content: Content, mapping_id: str, aws_version: str | None) -> dict[str, Any]:
        core = await self.read.core()
        body = {"apiVersion": "v1", "kind": "Secret", "type": content.type, "data": to_kubernetes(content),
                "metadata": {"name": name, "namespace": namespace, "labels": {MOKS_LABEL: "true"},
                             "annotations": {MAPPING_ANNOTATION: mapping_id, **({AWS_VERSION_ANNOTATION: aws_version} if aws_version else {})}}}
        return summary(await self._obj(await self._call(f"create secret {namespace}/{name}", core.create_namespaced_secret, namespace, body)))

    async def replace(self, namespace: str, name: str, content: Content, resource_version: str, uid: str, mapping_id: str, aws_version: str | None) -> dict[str, Any]:
        """Replace data only. Labels, annotations and owner references stay as they are; the type cannot change."""
        core = await self.read.core()
        obj = await self._obj(await self._call(f"read secret {namespace}/{name}", core.read_namespaced_secret, name, namespace))
        meta = obj["metadata"]
        if meta.get("uid") != uid or meta.get("resourceVersion") != resource_version:
            raise ConnectorError("conflict", f"secret {namespace}/{name} changed since the plan was approved")
        if (obj.get("type") or "Opaque") != content.type:
            raise ConnectorError("bad_request", "Kubernetes does not allow changing a Secret's type")
        meta.setdefault("annotations", {})[MAPPING_ANNOTATION] = mapping_id
        if aws_version:
            meta["annotations"][AWS_VERSION_ANNOTATION] = aws_version
        body = {"apiVersion": "v1", "kind": "Secret", "type": content.type, "data": to_kubernetes(content), "metadata": meta}
        if obj.get("immutable"):
            body["immutable"] = True
        out = summary(await self._obj(await self._call(f"update secret {namespace}/{name}", core.replace_namespaced_secret, name, namespace, body)))
        del obj, body
        return out

    async def delete(self, namespace: str, name: str, uid: str, resource_version: str) -> None:
        from kubernetes_asyncio import client

        core = await self.read.core()
        opts = client.V1DeleteOptions(preconditions=client.V1Preconditions(uid=uid, resource_version=resource_version))
        await self._call(f"delete secret {namespace}/{name}", core.delete_namespaced_secret, name, namespace, body=opts)

    async def workloads(self, namespace: str) -> list[dict[str, Any]]:
        """Pod templates in the namespace, for consumers_of. Bare pods only when no controller owns them."""
        apps = await self.read.apps()
        core = await self.read.core()
        out: list[dict[str, Any]] = []
        for kind, fn in (("Deployment", apps.list_namespaced_deployment), ("StatefulSet", apps.list_namespaced_stateful_set), ("DaemonSet", apps.list_namespaced_daemon_set)):
            try:
                body = await self._obj(await self._call(f"list {kind}s in {namespace}", fn, namespace, limit=500))
            except ConnectorError:
                continue  # impact is advisory; a missing permission must not block the review
            out += [{"kind": kind, "name": i["metadata"]["name"], "pod_spec": ((i.get("spec") or {}).get("template") or {}).get("spec") or {}} for i in body.get("items") or []]
        try:
            pods = await self._obj(await self._call(f"list pods in {namespace}", core.list_namespaced_pod, namespace, limit=500))
            out += [{"kind": "Pod", "name": p["metadata"]["name"], "pod_spec": p.get("spec") or {}} for p in pods.get("items") or [] if not p["metadata"].get("ownerReferences")]
        except ConnectorError:
            pass
        return out
