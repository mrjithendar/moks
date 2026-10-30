"""Write-side Kubernetes client used ONLY by the executor process (MOKS_PROCESS_ROLE=executor).

It authenticates as the executor's own ServiceAccount (moks-executor), which is bound per
namespace to exactly the verbs the allowlisted actions need. The API and the investigation worker
never construct it. Every mutation is guarded by the resourceVersion captured when the plan was
made (optimistic concurrency), so a resource that changed after review is not touched.
"""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
from typing import Any

from ..config import get_settings
from ..connectors.base import ConnectorError
from ..connectors.kubernetes import KubernetesConnector, _api_error, summarize_pod, summarize_workload

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
               "HorizontalPodAutoscaler": client.AutoscalingV2Api(api).read_namespaced_horizontal_pod_autoscaler}
        return api.sanitize_for_serialization(await self._call(f"get {kind} {ns}/{name}", fns[kind], name, ns))

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

    async def exec(self, ns: str, pod: str, argv: list[str], timeout: int = 30, max_bytes: int = 64_000) -> tuple[int | None, str]:
        """Run argv (no shell) in a diagnostic pod; returns (exit code, combined output, truncated)."""
        self._diag_ns(ns)
        from kubernetes_asyncio import client
        from kubernetes_asyncio.stream import WsApiClient

        cfg = (await self.read._client()).configuration
        async with WsApiClient(configuration=cfg) as ws:
            api = client.CoreV1Api(api_client=ws)
            try:
                resp = await asyncio.wait_for(api.connect_get_namespaced_pod_exec(
                    pod, ns, command=argv, stderr=True, stdout=True, stdin=False, tty=False, _preload_content=False), 10)
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


def summarize(pods: list[dict]) -> list[dict]:
    return pods
