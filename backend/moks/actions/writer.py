"""Write-side Kubernetes client used ONLY by the executor process (MOKS_PROCESS_ROLE=executor).

It authenticates as the executor's own ServiceAccount (moks-executor), which is bound per
namespace to exactly the verbs the allowlisted actions need. The API and the investigation worker
never construct it. Every mutation is guarded by the resourceVersion captured when the plan was
made (optimistic concurrency), so a resource that changed after review is not touched.
"""
from __future__ import annotations

import asyncio
import json
import copy
from datetime import datetime, timezone
from typing import Any

from .. import velero
from ..config import get_settings
from ..connectors.base import ConnectorError
from ..connectors.kubernetes import KubernetesConnector, _api_error, open_exec, summarize_pod, summarize_workload

STRATEGIC = "application/strategic-merge-patch+json"
MERGE = "application/merge-patch+json"


class KubernetesWriter:
    def __init__(self, config: dict[str, Any], credential_ref: dict[str, Any] | None = None):
        if get_settings().process_role != "executor":
            raise ConnectorError("forbidden", "write access to clusters exists only in the executor process")
        self.read = KubernetesConnector(config, credential_ref)

    async def aclose(self) -> None:
        await self.read.aclose()

    async def _apps(self):
        return await self.read.apps()

    async def _call(self, what: str, fn, *args, **kwargs):
        try:
            return await asyncio.wait_for(fn(*args, **kwargs), 30)
        except ConnectorError:
            raise
        except Exception as exc:
            if getattr(exc, "status", None) == 409:
                raise ConnectorError("conflict", f"{what}: the resource changed since it was read (409 conflict)") from None
            raise _api_error(exc, what) from None

    async def _custom(self):
        return await self.read.custom()

    async def velero_get(self, plural: str, name: str) -> dict:
        return await self.read.velero_get(plural, name)

    async def velero_create(self, plural: str, body: dict) -> dict:
        custom = await self._custom()
        return await self._call(f"create velero {plural}", custom.create_namespaced_custom_object, velero.GROUP, velero.VERSION, velero.namespace(), plural, body)

    async def velero_apply(self, plural: str, name: str, body: dict) -> dict:
        custom = await self._custom()
        try:
            return await self.velero_create(plural, body)
        except ConnectorError as exc:
            if exc.kind != "conflict":
                raise
        return await self._call(f"patch velero {plural}/{name}", custom.patch_namespaced_custom_object, velero.GROUP, velero.VERSION,
                                velero.namespace(), plural, name, {"spec": body["spec"]}, _content_type=MERGE)

    async def write_secret(self, name: str, data: dict[str, str], overwrite: bool) -> str:
        """Create or overwrite, and nothing else: the role has no get or list on Secrets, and values
        never appear in a result. An existing Secret is kept when overwrite is False."""
        core = await self.read.core()
        ns = velero.namespace()
        body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "data": data,
                "metadata": {"name": name, "namespace": ns, "labels": {"app.kubernetes.io/managed-by": "moks"}}}
        try:
            await self._call(f"create secret {name}", core.create_namespaced_secret, ns, body)
            return "created"
        except ConnectorError as exc:
            if exc.kind != "conflict":
                raise
        if not overwrite:
            return "kept"
        await self._call(f"update secret {name}", core.patch_namespaced_secret, name, ns, {"data": data}, _content_type=MERGE)
        return "updated"

    async def deployment(self, ns: str, name: str) -> dict:
        apps = await self._apps()
        api = await self.read._client()
        return api.sanitize_for_serialization(await self._call(f"get deployment {ns}/{name}", apps.read_namespaced_deployment, name, ns))

    async def state(self, ns: str, name: str) -> dict:
        d = await self.deployment(ns, name)
        return {"resourceVersion": d["metadata"]["resourceVersion"], "generation": d["metadata"].get("generation"),
                "replicas": d["spec"].get("replicas"), "template": d["spec"]["template"],
                "revision": (d["metadata"].get("annotations") or {}).get("deployment.kubernetes.io/revision"),
                "summary": summarize_workload("Deployment", d)}

    async def _replace_spec(self, ns: str, name: str, resource_version: str, mutate) -> dict:
        d = await self.deployment(ns, name)
        if d["metadata"]["resourceVersion"] != resource_version:
            raise ConnectorError("conflict", f"deployment {ns}/{name} changed since the plan was approved "
                                             f"(resourceVersion {resource_version} -> {d['metadata']['resourceVersion']})")
        mutate(d)
        apps = await self._apps()
        api = await self.read._client()
        out = await self._call(f"replace deployment {ns}/{name}", apps.replace_namespaced_deployment, name, ns, d)
        return api.sanitize_for_serialization(out)

    async def restart(self, ns: str, name: str, resource_version: str) -> dict:
        stamp = datetime.now(timezone.utc).isoformat()

        def mutate(d):
            tmpl_meta = d["spec"]["template"].setdefault("metadata", {})
            tmpl_meta.setdefault("annotations", {})["kubectl.kubernetes.io/restartedAt"] = stamp
        await self._replace_spec(ns, name, resource_version, mutate)
        return {"restartedAt": stamp}

    async def scale(self, ns: str, name: str, resource_version: str, replicas: int) -> dict:
        def mutate(d):
            d["spec"]["replicas"] = replicas
        await self._replace_spec(ns, name, resource_version, mutate)
        return {"replicas": replicas}

    async def rollback(self, ns: str, name: str, resource_version: str, to_revision: int) -> dict:
        apps = await self._apps()
        api = await self.read._client()
        d = await self.deployment(ns, name)
        selector = ",".join(f"{k}={v}" for k, v in (d["spec"]["selector"].get("matchLabels") or {}).items())
        rss = api.sanitize_for_serialization(await self._call("list replicasets", apps.list_namespaced_replica_set, ns, label_selector=selector))
        target = next((rs for rs in rss.get("items", []) if
                       any(o.get("uid") == d["metadata"]["uid"] for o in rs["metadata"].get("ownerReferences") or []) and
                       (rs["metadata"].get("annotations") or {}).get("deployment.kubernetes.io/revision") == str(to_revision)), None)
        if target is None:
            raise ConnectorError("not_found", f"revision {to_revision} of {ns}/{name} no longer exists")
        tmpl = copy.deepcopy(target["spec"]["template"])
        (tmpl.get("metadata") or {}).get("labels", {}).pop("pod-template-hash", None)

        def mutate(dd):
            dd["spec"]["template"] = tmpl
        await self._replace_spec(ns, name, resource_version, mutate)
        return {"rolled_back_to": to_revision, "images": [c.get("image") for c in tmpl["spec"].get("containers", [])]}

    async def patch_resources(self, ns: str, name: str, resource_version: str, container: str, resources: dict) -> dict:
        def mutate(d):
            for c in d["spec"]["template"]["spec"]["containers"]:
                if c["name"] == container:
                    cur = c.setdefault("resources", {})
                    for section in ("requests", "limits"):
                        if resources.get(section):
                            cur.setdefault(section, {}).update(resources[section])
                    return
            raise ConnectorError("not_found", f"container {container} not in {ns}/{name}")
        await self._replace_spec(ns, name, resource_version, mutate)
        return {"container": container, "resources": resources}

    async def merge_patch(self, ns: str, kind: str, name: str, resource_version: str, patch: dict) -> dict:
        from kubernetes_asyncio import client

        api = await self.read._client()
        body = copy.deepcopy(patch)
        body.setdefault("metadata", {})["resourceVersion"] = resource_version  # 409 if changed since review
        fns = {"Deployment": client.AppsV1Api(api).patch_namespaced_deployment, "ConfigMap": client.CoreV1Api(api).patch_namespaced_config_map,
               "Service": client.CoreV1Api(api).patch_namespaced_service,
               "HorizontalPodAutoscaler": client.AutoscalingV2Api(api).patch_namespaced_horizontal_pod_autoscaler}
        out = await self._call(f"patch {kind} {ns}/{name}", fns[kind], name, ns, body, _content_type=MERGE)
        return {"resourceVersion": api.sanitize_for_serialization(out)["metadata"]["resourceVersion"]}

    async def get_object(self, ns: str, kind: str, name: str) -> dict:
        from kubernetes_asyncio import client

        api = await self.read._client()
        fns = {"Deployment": client.AppsV1Api(api).read_namespaced_deployment, "ConfigMap": client.CoreV1Api(api).read_namespaced_config_map,
               "Service": client.CoreV1Api(api).read_namespaced_service,
               "HorizontalPodAutoscaler": client.AutoscalingV2Api(api).read_namespaced_horizontal_pod_autoscaler,
               "Pod": client.CoreV1Api(api).read_namespaced_pod}
        return api.sanitize_for_serialization(await self._call(f"get {kind} {ns}/{name}", fns[kind], name, ns))

    async def delete_managed_pod(self, ns: str, name: str, uid: str) -> dict:
        """The UID precondition makes the API server refuse if the name now belongs to another pod."""
        from kubernetes_asyncio import client

        core = await self.read.core()
        await self._call(f"delete pod {ns}/{name}", core.delete_namespaced_pod, name, ns, body=client.V1DeleteOptions(preconditions=client.V1Preconditions(uid=uid)))
        return {"deleted": name, "uid": uid}

    async def cluster_uid(self) -> str:
        return await self.read.cluster_uid()

    async def services(self, ns: str) -> list[dict]:
        return await self.read.services(ns)

    async def pods_of(self, ns: str, name: str) -> list[dict]:
        """Pods of the Deployment's current revision, followed through ownerReferences. Pods of old
        ReplicaSets and terminating pods say nothing about whether this revision works."""
        apps = await self._apps()
        api = await self.read._client()
        d = await self.deployment(ns, name)
        selector = ",".join(f"{k}={v}" for k, v in (d["spec"]["selector"].get("matchLabels") or {}).items())
        revision = (d["metadata"].get("annotations") or {}).get("deployment.kubernetes.io/revision")
        rss = api.sanitize_for_serialization(await self._call("list replicasets", apps.list_namespaced_replica_set, ns, label_selector=selector))
        current = {rs["metadata"]["uid"] for rs in rss.get("items", [])
                   if any(o.get("uid") == d["metadata"]["uid"] for o in rs["metadata"].get("ownerReferences") or [])
                   and (rs["metadata"].get("annotations") or {}).get("deployment.kubernetes.io/revision") == revision}
        pods = await self.read.raw_pods(ns)
        return [summarize_pod(p) for p in pods if not p["metadata"].get("deletionTimestamp")
                and any(o.get("uid") in current for o in p["metadata"].get("ownerReferences") or [])]

    # ---- diagnostic pods (namespace MOKS_DIAGNOSTICS_NAMESPACE only) ------------------------

    def _diag_ns(self, ns: str) -> None:
        if ns != get_settings().diagnostics_namespace:
            raise ConnectorError("forbidden", "diagnostic pods may only run in the diagnostics namespace")

    async def create_pod(self, ns: str, manifest: dict) -> str:
        self._diag_ns(ns)
        core = await self.read.core()
        out = await self._call("create diagnostic pod", core.create_namespaced_pod, ns, manifest)
        return out.metadata.name

    async def pod_phase(self, ns: str, name: str) -> str:
        self._diag_ns(ns)
        core = await self.read.core()
        p = await self._call("get diagnostic pod", core.read_namespaced_pod, name, ns)
        return p.status.phase

    async def delete_pod(self, ns: str, name: str) -> None:
        self._diag_ns(ns)
        core = await self.read.core()
        try:
            await self._call("delete diagnostic pod", core.delete_namespaced_pod, name, ns, grace_period_seconds=0)
        except ConnectorError as exc:
            if exc.kind != "not_found":
                raise

    async def database_pod(self, ns: str, kind: str, name: str) -> dict[str, str]:
        """A ready pod of a database workload, and the container running the database."""
        from .. import dbbackup

        apps = await self._apps()
        api = await self.read._client()
        read = apps.read_namespaced_stateful_set if kind == "StatefulSet" else apps.read_namespaced_deployment
        w = api.sanitize_for_serialization(await self._call(f"get {kind} {ns}/{name}", read, name, ns))
        labels = (w["spec"]["selector"].get("matchLabels") or {}).items()
        for p in await self.read.raw_pods(ns):
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in (p.get("status") or {}).get("conditions") or [])
            if not ready or p["metadata"].get("deletionTimestamp") or any((p["metadata"].get("labels") or {}).get(k) != v for k, v in labels):
                continue
            for c in p["spec"]["containers"]:
                if dbbackup.engine_of(c.get("image") or ""):
                    return {"name": p["metadata"]["name"], "container": c["name"], "image": c["image"]}
        raise ConnectorError("conflict", f"{kind} {ns}/{name} has no ready database pod")

    async def exec_stream(self, ns: str, pod: str, container: str, argv: list[str], sink, timeout: int) -> tuple[int | None, str]:
        """Runs a fixed command and hands its stdout to sink as it arrives. Returns the exit code, None
        when the stream ended without one, and the end of stderr."""
        from kubernetes_asyncio.stream import WsApiClient

        cfg = (await self.read._client()).configuration
        async with WsApiClient(configuration=cfg) as ws:
            try:
                resp = await asyncio.wait_for(open_exec(ws, pod, ns, command=argv, container=container, stderr=True, stdout=True, stdin=False, tty=False), 15)
            except Exception as exc:
                raise _api_error(exc, f"exec in {ns}/{pod}") from None
            err, code = bytearray(), None
            try:
                async with asyncio.timeout(timeout):
                    while True:
                        msg = await resp.receive()
                        if msg.type not in (1, 2):  # aiohttp WSMsgType TEXT/BINARY
                            break
                        data = msg.data if isinstance(msg.data, (bytes, bytearray)) else msg.data.encode()
                        channel, payload = data[0], bytes(data[1:])
                        if channel == 1:
                            await sink(payload)
                        elif channel == 2:
                            err = (err + payload)[-4000:]
                        elif channel == 3:
                            code = _exit_code(payload)
            except TimeoutError:
                err.extend(f"\n[timed out after {timeout}s]".encode())
            finally:
                await resp.close()
        return code, err.decode("utf-8", "replace")

    async def exec(self, ns: str, pod: str, argv: list[str], timeout: int = 30, max_bytes: int = 64_000) -> tuple[int | None, str]:
        """Run argv (no shell) in a diagnostic pod; returns (exit code, combined output, truncated)."""
        self._diag_ns(ns)
        from kubernetes_asyncio.stream import WsApiClient

        cfg = (await self.read._client()).configuration
        async with WsApiClient(configuration=cfg) as ws:
            try:
                resp = await asyncio.wait_for(open_exec(ws, pod, ns, command=argv, stderr=True, stdout=True, stdin=False, tty=False), 10)
            except Exception as exc:
                raise _api_error(exc, "exec in diagnostic pod") from None
            out, code = bytearray(), None
            try:
                async with asyncio.timeout(timeout):
                    while True:
                        msg = await resp.receive()
                        if msg.type in (1, 2):  # aiohttp WSMsgType TEXT/BINARY
                            data = msg.data if isinstance(msg.data, (bytes, bytearray)) else msg.data.encode()
                            channel, payload = data[0], data[1:]
                            if channel in (1, 2) and len(out) < max_bytes:
                                out.extend(payload[: max_bytes - len(out)])
                            elif channel == 3:
                                import json as _json

                                status = _json.loads(payload.decode() or "{}")
                                code = 0 if status.get("status") == "Success" else int(
                                    next((c.get("message") for c in (status.get("details") or {}).get("causes", []) if c.get("reason") == "ExitCode"), 1))
                        else:
                            break
            except TimeoutError:
                out.extend(f"\n[timed out after {timeout}s]".encode())
            finally:
                await resp.close()
        return code, out.decode("utf-8", "replace")


def _exit_code(payload: bytes) -> int:
    status = json.loads(payload.decode() or "{}")
    if status.get("status") == "Success":
        return 0
    return int(next((c.get("message") for c in (status.get("details") or {}).get("causes", []) if c.get("reason") == "ExitCode"), 1))


def summarize(pods: list[dict]) -> list[dict]:
    return pods
