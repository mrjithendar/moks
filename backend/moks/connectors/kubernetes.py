"""Kubernetes connector for Minikube and EKS (in-cluster, kubeconfig or a short-lived EKS token).

Read-only by construction: this class has no methods that create, patch or delete, and none
that read Secrets. Its in-cluster ServiceAccount is bound to a ClusterRole without Secret
access (deploy/helm/moks/templates/rbac.yaml), so a bug here still cannot read them.

Results are compact summaries (not raw objects) so evidence stays small and explainable.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from .. import argocd, velero
from ..redaction import redact
from .base import ConnectorError, resolve_credential_path

LIST_PAGE = 250
# Literal env values under names like these are masked: a value alone ("s3cr3t") has no
# recognisable shape, so pattern redaction cannot catch it.
SENSITIVE_ENV = re.compile(r"(?i)(pass|secret|token|key|credential|auth|cert|private|dsn|salt|signature|cookie|session)")
MAX_ITEMS = 1000


def _api_error(exc: Exception, what: str) -> ConnectorError:
    from aiohttp import ClientError
    from kubernetes_asyncio.client.exceptions import ApiException

    if isinstance(exc, ApiException):
        if exc.status == 403:
            return ConnectorError("forbidden", f"Kubernetes RBAC denied {what} for MOKS's service account")
        if exc.status == 401:
            return ConnectorError("auth", "Kubernetes rejected MOKS's credentials (401)")
        if exc.status == 404:
            return ConnectorError("not_found", f"{what}: not found")
        if exc.status and exc.status >= 500 or exc.status == 429:
            return ConnectorError("unavailable", f"Kubernetes API error {exc.status} during {what}", retryable=True)
        return ConnectorError("bad_request", f"Kubernetes rejected {what} ({exc.status}: {exc.reason})")
    if isinstance(exc, asyncio.TimeoutError):
        return ConnectorError("timeout", f"Kubernetes API timed out during {what}", retryable=True)
    if isinstance(exc, ClientError):
        return ConnectorError("unavailable", f"cannot reach the Kubernetes API ({type(exc).__name__})", retryable=True)
    return ConnectorError("unavailable", f"{what} failed ({type(exc).__name__})")


async def open_exec(ws_client, pod: str, ns: str, **kwargs):
    """An open exec WebSocket. The client returns aiohttp's connection context, and awaiting that is what connects."""
    from kubernetes_asyncio import client

    connecting = await client.CoreV1Api(api_client=ws_client).connect_get_namespaced_pod_exec(pod, ns, _preload_content=False, **kwargs)
    return await connecting


class KubernetesConnector:
    type = "kubernetes"

    def __init__(self, config: dict[str, Any], credential_ref: dict[str, Any] | None = None):
        self.config = config
        self.credential_ref = credential_ref
        self.timeout = float(config.get("timeout_seconds", 15))
        self._api = None
        self._lock = asyncio.Lock()
        self._api_groups: set[str] | None = None
        self._token_expires: float | None = None
        self._ca_file: str | None = None

    async def _configure_eks(self, cfg) -> None:
        """Remote EKS: endpoint and CA from eks:DescribeCluster, bearer token from a presigned STS
        GetCallerIdentity URL (the same mechanism as `aws eks get-token`), refreshed every 10 minutes."""
        cluster = self.config.get("cluster_name")
        region = self.config.get("region") or "us-east-1"
        if not cluster:
            raise ConnectorError("config", "eks mode needs config.cluster_name and config.region")
        info, token = await asyncio.to_thread(eks_endpoint_and_token, cluster, region, self.config.get("role_arn"))
        import base64
        import tempfile

        if not self._ca_file:
            fd, self._ca_file = tempfile.mkstemp(prefix="moks-eks-ca-", suffix=".crt")
            with os.fdopen(fd, "wb") as f:
                f.write(base64.b64decode(info["ca"]))
        cfg.host = info["endpoint"]
        cfg.ssl_ca_cert = self._ca_file
        cfg.api_key = {"authorization": f"Bearer {token}"}
        self._token_expires = time.monotonic() + 600

    async def _client(self):
        async with self._lock:
            if self._api is not None and not (self._token_expires and time.monotonic() > self._token_expires):
                return self._api
            if self._api is not None:  # EKS token about to expire: rebuild the client
                await self._api.close()
                self._api = None
            from kubernetes_asyncio import client, config as kconfig

            cfg = client.Configuration()
            mode = self.config.get("mode", "in_cluster")
            try:
                if mode == "eks":
                    await self._configure_eks(cfg)
                elif mode == "in_cluster":
                    kconfig.load_incluster_config(client_configuration=cfg)
                elif mode == "kubeconfig":
                    if not self.credential_ref or self.credential_ref.get("type") != "file":
                        raise ConnectorError("config", "kubeconfig mode needs a file credential reference to the kubeconfig")
                    path = resolve_credential_path(self.credential_ref)
                    await kconfig.load_kube_config(config_file=str(path), context=self.config.get("context") or None,
                                                   client_configuration=cfg)
                else:
                    raise ConnectorError("config", f"unknown kubernetes mode {mode!r} (in_cluster | kubeconfig | eks)")
            except ConnectorError:
                raise
            except Exception as exc:
                raise ConnectorError("config", f"cannot load Kubernetes credentials ({type(exc).__name__}: {str(exc)[:200]})") from None
            self._api = client.ApiClient(configuration=cfg)
            return self._api

    async def aclose(self) -> None:
        if self._api is not None:
            await self._api.close()
            self._api = None

    async def _call(self, what: str, fn, *args, **kwargs):
        try:
            return await asyncio.wait_for(fn(*args, _request_timeout=self.timeout, **kwargs), self.timeout + 2)
        except ConnectorError:
            raise
        except Exception as exc:
            raise _api_error(exc, what) from None

    async def _list(self, what: str, fn, *args, **kwargs) -> tuple[list[dict], bool]:
        """Paginated list; returns (items as dicts, truncated)."""
        api = await self._client()
        items: list[dict] = []
        cont = None
        for _ in range(MAX_ITEMS // LIST_PAGE + 1):
            resp = await self._call(what, fn, *args, limit=LIST_PAGE, _continue=cont, **kwargs)
            data = api.sanitize_for_serialization(resp)
            items.extend(data.get("items") or [])
            cont = (data.get("metadata") or {}).get("continue")
            if not cont or len(items) >= MAX_ITEMS:
                break
        return items[:MAX_ITEMS], bool(cont)

    # ---- API objects --------------------------------------------------------------------

    async def core(self):
        from kubernetes_asyncio import client

        return client.CoreV1Api(await self._client())

    async def apps(self):
        from kubernetes_asyncio import client

        return client.AppsV1Api(await self._client())

    async def custom(self):
        from kubernetes_asyncio import client

        return client.CustomObjectsApi(await self._client())

    # ---- Velero (its own objects only; these hold names and counts, never Secret data) ---

    async def velero_list(self, plural: str) -> list[dict]:
        custom = await self.custom()
        items, _ = await self._list(f"list velero {plural}", custom.list_namespaced_custom_object, velero.GROUP, velero.VERSION, velero.namespace(), plural)
        return items

    async def velero_get(self, plural: str, name: str) -> dict:
        custom = await self.custom()
        return await self._call(f"get velero {plural}/{name}", custom.get_namespaced_custom_object, velero.GROUP, velero.VERSION, velero.namespace(), plural, name)

    # ---- Argo CD (Application status; the moks.argocd row keeps no Helm parameters or values) ---

    async def argocd_list(self) -> list[dict]:
        custom = await self.custom()
        items, _ = await self._list("list argo cd applications", custom.list_namespaced_custom_object, argocd.GROUP, argocd.VERSION, argocd.namespace(), argocd.PLURAL)
        return items

    # ---- Discovery ----------------------------------------------------------------------

    async def api_groups(self) -> set[str]:
        if self._api_groups is None:
            from kubernetes_asyncio import client

            api = await self._client()
            groups = await self._call("API discovery", client.ApisApi(api).get_api_versions)
            self._api_groups = {g.name for g in groups.groups}
        return self._api_groups

    async def test(self) -> str:
        from kubernetes_asyncio import client

        api = await self._client()
        version = await self._call("version", client.VersionApi(api).get_code)
        groups = await self.api_groups()
        return f"Kubernetes {version.git_version} reachable; {len(groups)} API groups"

    async def server_version(self) -> str:
        from kubernetes_asyncio import client

        api = await self._client()
        v = await self._call("version", client.VersionApi(api).get_code)
        return v.git_version

    # ---- Reads --------------------------------------------------------------------------

    async def list_namespaces(self) -> list[str]:
        core = await self.core()
        items, _ = await self._list("list namespaces", core.list_namespace)
        return sorted(i["metadata"]["name"] for i in items)

    async def list_pods(self, namespace: str, label_selector: str | None = None) -> tuple[list[dict], bool]:
        core = await self.core()
        kw = {"label_selector": label_selector} if label_selector else {}
        items, truncated = await self._list(f"list pods in {namespace}", core.list_namespaced_pod, namespace, **kw)
        return [summarize_pod(p) for p in items], truncated

    async def raw_pods(self, namespace: str) -> list[dict]:
        core = await self.core()
        items, _ = await self._list(f"list pods in {namespace}", core.list_namespaced_pod, namespace)
        return items

    async def pvcs(self, namespace: str) -> list[dict]:
        core = await self.core()
        items, _ = await self._list(f"list pvcs in {namespace}", core.list_namespaced_persistent_volume_claim, namespace)
        return [{"name": i["metadata"]["name"], "storage_class": i["spec"].get("storageClassName"),
                 "request": ((i["spec"].get("resources") or {}).get("requests") or {}).get("storage"),
                 "capacity": (i.get("status") or {}).get("capacity", {}).get("storage"), "phase": (i.get("status") or {}).get("phase")}
                for i in items]

    async def describe_pod(self, namespace: str, name: str) -> dict:
        core = await self.core()
        api = await self._client()
        pod = api.sanitize_for_serialization(await self._call(f"get pod {namespace}/{name}", core.read_namespaced_pod, name, namespace))
        events = await self.events(namespace, involved_name=name, involved_kind="Pod", warnings_only=False, limit=30)
        return {"summary": summarize_pod(pod), "containers": describe_containers(pod), "conditions": pod_conditions(pod),
                "node": pod.get("spec", {}).get("nodeName"), "events": events}

    async def pod_logs(self, namespace: str, pod: str, container: str | None, previous: bool,
                       tail_lines: int, since_seconds: int | None) -> str:
        core = await self.core()
        kw: dict[str, Any] = {"tail_lines": tail_lines, "previous": previous, "timestamps": True, "limit_bytes": 256_000}
        if container:
            kw["container"] = container
        if since_seconds:
            kw["since_seconds"] = since_seconds
        try:
            text = await self._call(f"read logs of {namespace}/{pod}", core.read_namespaced_pod_log, pod, namespace, **kw)
        except ConnectorError as exc:
            if exc.kind == "bad_request" and previous:
                raise ConnectorError("not_found", f"no previous container instance for {namespace}/{pod}") from None
            raise
        return text or ""

    async def events(self, namespace: str, involved_name: str | None = None, involved_kind: str | None = None,
                     warnings_only: bool = True, limit: int = 100) -> list[dict]:
        core = await self.core()
        selectors = []
        if involved_name:
            selectors.append(f"involvedObject.name={involved_name}")
        if involved_kind:
            selectors.append(f"involvedObject.kind={involved_kind}")
        if warnings_only:
            selectors.append("type=Warning")
        kw = {"field_selector": ",".join(selectors)} if selectors else {}
        items, _ = await self._list(f"list events in {namespace}", core.list_namespaced_event, namespace, **kw)
        out = [summarize_event(e) for e in items]
        out.sort(key=lambda e: e.get("lastSeen") or "", reverse=True)
        return out[:limit]

    async def workloads(self, namespace: str) -> list[dict]:
        apps = await self.apps()
        deps, _ = await self._list(f"list deployments in {namespace}", apps.list_namespaced_deployment, namespace)
        sts, _ = await self._list(f"list statefulsets in {namespace}", apps.list_namespaced_stateful_set, namespace)
        dss, _ = await self._list(f"list daemonsets in {namespace}", apps.list_namespaced_daemon_set, namespace)
        return [summarize_workload("Deployment", d) for d in deps] + [summarize_workload("StatefulSet", s) for s in sts] + \
            [summarize_workload("DaemonSet", d) for d in dss]

    async def rollout_history(self, namespace: str, deployment: str) -> dict:
        apps = await self.apps()
        api = await self._client()
        dep = api.sanitize_for_serialization(await self._call(f"get deployment {namespace}/{deployment}", apps.read_namespaced_deployment, deployment, namespace))
        selector = dep.get("spec", {}).get("selector", {}).get("matchLabels") or {}
        label_selector = ",".join(f"{k}={v}" for k, v in selector.items())
        rss, _ = await self._list(f"list replicasets in {namespace}", apps.list_namespaced_replica_set, namespace, label_selector=label_selector)
        uid = dep["metadata"].get("uid")
        revisions = []
        for rs in rss:
            if not any(o.get("uid") == uid for o in rs["metadata"].get("ownerReferences") or []):
                continue
            ann = rs["metadata"].get("annotations") or {}
            revisions.append({
                "revision": int(ann.get("deployment.kubernetes.io/revision", "0") or 0),
                "replicaSet": rs["metadata"]["name"],
                "created": rs["metadata"].get("creationTimestamp"),
                "images": [c.get("image") for c in rs["spec"]["template"]["spec"].get("containers", [])],
                "replicas": rs.get("status", {}).get("replicas", 0),
                "readyReplicas": rs.get("status", {}).get("readyReplicas", 0),
                "changeCause": ann.get("kubernetes.io/change-cause"),
                "podTemplateHash": (rs["metadata"].get("labels") or {}).get("pod-template-hash"),
            })
        revisions.sort(key=lambda r: r["revision"], reverse=True)
        return {"deployment": summarize_workload("Deployment", dep), "revisions": revisions[:15]}

    async def cluster_uid(self) -> str:
        """UID of kube-system: stable for a cluster's lifetime and unique across clusters."""
        core = await self.core()
        ns = await self._call("get namespace kube-system", core.read_namespace, "kube-system")
        return ns.metadata.uid

    async def deployment_meta(self, namespace: str, name: str) -> dict:
        apps = await self.apps()
        api = await self._client()
        dep = api.sanitize_for_serialization(await self._call(f"get deployment {namespace}/{name}", apps.read_namespaced_deployment, name, namespace))
        tmpl = dep.get("spec", {}).get("template", {})
        return {
            "name": name, "namespace": namespace,
            "resourceVersion": dep["metadata"].get("resourceVersion"), "uid": dep["metadata"].get("uid"), "generation": dep["metadata"].get("generation"),
            "annotations": {**(dep["metadata"].get("annotations") or {}), **((tmpl.get("metadata") or {}).get("annotations") or {})},
            "labels": dep["metadata"].get("labels") or {},
            "images": [c.get("image") for c in tmpl.get("spec", {}).get("containers", [])],
            "replicas": dep.get("spec", {}).get("replicas"),
            "summary": summarize_workload("Deployment", dep),
        }

    async def object_meta(self, namespace: str, kind: str, name: str) -> dict:
        """resourceVersion and spec of a patchable object (Deployment, Service, HPA)."""
        from kubernetes_asyncio import client

        api = await self._client()
        fns = {"Deployment": client.AppsV1Api(api).read_namespaced_deployment, "StatefulSet": client.AppsV1Api(api).read_namespaced_stateful_set, "Service": client.CoreV1Api(api).read_namespaced_service,
               "HorizontalPodAutoscaler": client.AutoscalingV2Api(api).read_namespaced_horizontal_pod_autoscaler, "Pod": client.CoreV1Api(api).read_namespaced_pod}
        if kind not in fns:
            raise ConnectorError("bad_request", f"unsupported kind {kind}")
        obj = api.sanitize_for_serialization(await self._call(f"get {kind} {namespace}/{name}", fns[kind], name, namespace))
        return {"kind": kind, "name": name, "resourceVersion": obj["metadata"].get("resourceVersion"), "uid": obj["metadata"].get("uid"), "spec": obj.get("spec", {}),
                "owner": next((f"{o['kind']}/{o['name']}" for o in obj["metadata"].get("ownerReferences") or [] if o.get("controller")), None),
                "generation": obj["metadata"].get("generation")}

    async def services(self, namespace: str) -> list[dict]:
        """Services with their selectors, ports, ready endpoint counts and matching pods."""
        from kubernetes_asyncio import client

        core = await self.core()
        api = await self._client()
        svcs, _ = await self._list(f"list services in {namespace}", core.list_namespaced_service, namespace)
        slices, _ = await self._list(f"list endpointslices in {namespace}", client.DiscoveryV1Api(api).list_namespaced_endpoint_slice, namespace)
        pods = await self.raw_pods(namespace)
        by_service: dict[str, dict[str, int]] = {}
        for sl in slices:
            svc = (sl["metadata"].get("labels") or {}).get("kubernetes.io/service-name")
            if not svc:
                continue
            agg = by_service.setdefault(svc, {"ready": 0, "notReady": 0})
            for ep in sl.get("endpoints") or []:
                ready = (ep.get("conditions") or {}).get("ready")
                agg["ready" if ready is not False else "notReady"] += len(ep.get("addresses") or [])
        out = []
        for s in svcs:
            spec = s.get("spec", {})
            selector = spec.get("selector") or {}
            matching = [p for p in pods if selector and all((p["metadata"].get("labels") or {}).get(k) == v for k, v in selector.items())]
            name = s["metadata"]["name"]
            out.append({
                "name": name,
                "type": spec.get("type"),
                "selector": selector,
                "ports": [{"name": p.get("name"), "port": p.get("port"), "targetPort": p.get("targetPort"), "protocol": p.get("protocol")} for p in spec.get("ports") or []],
                "endpoints": by_service.get(name, {"ready": 0, "notReady": 0}),
                "matchingPods": [p["metadata"]["name"] for p in matching][:20],
                "matchingPodCount": len(matching),
                "podContainerPorts": sorted({(cp.get("name") or "", cp.get("containerPort")) for p in matching
                                             for c in p["spec"].get("containers", []) for cp in c.get("ports") or []},
                                            key=lambda t: (t[0], t[1] or 0))[:20],
                "nearMissPods": _near_miss(selector, pods) if selector and not matching else [],
            })
        return out

    async def ingresses(self, namespace: str) -> list[dict]:
        from kubernetes_asyncio import client

        api = await self._client()
        items, _ = await self._list(f"list ingresses in {namespace}", client.NetworkingV1Api(api).list_namespaced_ingress, namespace)
        out = []
        for ing in items:
            spec = ing.get("spec", {})
            rules = []
            for r in spec.get("rules") or []:
                for p in (r.get("http") or {}).get("paths") or []:
                    svc = (p.get("backend") or {}).get("service") or {}
                    rules.append({"host": r.get("host"), "path": p.get("path"), "pathType": p.get("pathType"),
                                  "service": svc.get("name"), "port": (svc.get("port") or {}).get("number") or (svc.get("port") or {}).get("name")})
            out.append({"name": ing["metadata"]["name"], "class": spec.get("ingressClassName"),
                        "annotations": {k: v for k, v in (ing["metadata"].get("annotations") or {}).items() if "traefik" in k or "ingress" in k},
                        "rules": rules, "tls": [t.get("hosts") for t in spec.get("tls") or []]})
        return out

    async def nodes(self) -> list[dict]:
        core = await self.core()
        items, _ = await self._list("list nodes", core.list_node)
        out = []
        for n in items:
            st = n.get("status", {})
            conds = {c["type"]: c.get("status") for c in st.get("conditions") or []}
            out.append({
                "name": n["metadata"]["name"],
                "ready": conds.get("Ready") == "True",
                "pressure": [t for t in ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable") if conds.get(t) == "True"],
                "unschedulable": bool(n.get("spec", {}).get("unschedulable")),
                "taints": [f"{t.get('key')}={t.get('value', '')}:{t.get('effect')}" for t in n.get("spec", {}).get("taints") or []],
                "capacity": {k: st.get("capacity", {}).get(k) for k in ("cpu", "memory", "pods")},
                "allocatable": {k: st.get("allocatable", {}).get(k) for k in ("cpu", "memory", "pods")},
                "kubelet": (st.get("nodeInfo") or {}).get("kubeletVersion"),
                "arch": (st.get("nodeInfo") or {}).get("architecture"),
            })
        return out

    async def dns_health(self) -> dict:
        """CoreDNS deployment and endpoints in kube-system (DNS issue triage)."""
        apps = await self.apps()
        api = await self._client()
        try:
            dep = api.sanitize_for_serialization(await self._call("get coredns", apps.read_namespaced_deployment, "coredns", "kube-system"))
            workload = summarize_workload("Deployment", dep)
        except ConnectorError as exc:
            workload = {"error": exc.message}
        svcs = [s for s in await self.services("kube-system") if s["name"] in ("kube-dns", "coredns")]
        pods, _ = await self.list_pods("kube-system", "k8s-app=kube-dns")
        return {"deployment": workload, "services": svcs, "pods": pods}


# ---- Summaries -----------------------------------------------------------------------------

def _age(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit}"
    return f"{max(secs, 0)}s"


def summarize_pod(p: dict) -> dict:
    meta, spec, st = p.get("metadata", {}), p.get("spec", {}), p.get("status", {})
    statuses = st.get("containerStatuses") or []
    init_statuses = st.get("initContainerStatuses") or []
    problems = []
    for c in init_statuses + statuses:
        state = c.get("state") or {}
        last = (c.get("lastState") or {}).get("terminated")
        if state.get("waiting"):
            w = state["waiting"]
            problems.append({"container": c["name"], "state": "waiting", "reason": w.get("reason"), "message": redact((w.get("message") or "")[:300])})
        if state.get("terminated") and state["terminated"].get("exitCode", 0) != 0:
            t = state["terminated"]
            problems.append({"container": c["name"], "state": "terminated", "reason": t.get("reason"), "exitCode": t.get("exitCode"), "finishedAt": t.get("finishedAt")})
        if last:
            problems.append({"container": c["name"], "state": "lastTerminated", "reason": last.get("reason"), "exitCode": last.get("exitCode"), "finishedAt": last.get("finishedAt")})
    unscheduled = next((c for c in st.get("conditions") or [] if c.get("type") == "PodScheduled" and c.get("status") == "False"), None)
    if unscheduled:
        problems.append({"container": None, "state": "unschedulable", "reason": unscheduled.get("reason"), "message": (unscheduled.get("message") or "")[:300]})
    owner = (meta.get("ownerReferences") or [{}])[0]
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "phase": st.get("phase"),
        "ready": f"{sum(1 for c in statuses if c.get('ready'))}/{len(spec.get('containers', []))}",
        "restarts": sum(c.get("restartCount", 0) for c in statuses),
        "node": spec.get("nodeName"),
        "age": _age(meta.get("creationTimestamp")),
        "created": meta.get("creationTimestamp"),
        "owner": f"{owner.get('kind')}/{owner.get('name')}" if owner.get("kind") else None,
        "labels": meta.get("labels") or {},
        "problems": problems,
        "reason": st.get("reason"),
    }


def describe_containers(p: dict) -> list[dict]:
    statuses = {c["name"]: c for c in (p.get("status", {}).get("containerStatuses") or [])}
    out = []
    for c in p.get("spec", {}).get("containers", []):
        s = statuses.get(c["name"], {})
        env = []
        for e in c.get("env") or []:
            if "valueFrom" in e:
                vf = e["valueFrom"]
                src = next(iter(vf.keys()), "ref")
                ref = vf.get(src) or {}
                env.append({"name": e["name"], "from": f"{src}:{ref.get('name', '')}/{ref.get('key', ref.get('fieldPath', ''))}"})
            else:
                value = "[REDACTED]" if SENSITIVE_ENV.search(e["name"]) else redact(str(e.get("value", ""))[:200])
                env.append({"name": e["name"], "value": value})
        out.append({
            "name": c["name"],
            "image": c.get("image"),
            "imageID": s.get("imageID"),
            "ports": c.get("ports") or [],
            "resources": c.get("resources") or {},
            "livenessProbe": c.get("livenessProbe"),
            "readinessProbe": c.get("readinessProbe"),
            "startupProbe": c.get("startupProbe"),
            "env": env[:60],
            "envFrom": [{k: (v or {}).get("name") for k, v in ef.items() if k in ("configMapRef", "secretRef")} for ef in c.get("envFrom") or []],
            "state": s.get("state"),
            "lastState": s.get("lastState"),
            "ready": s.get("ready"),
            "restartCount": s.get("restartCount"),
        })
    return out


def pod_conditions(p: dict) -> list[dict]:
    return [{"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason"), "message": (c.get("message") or "")[:300]}
            for c in p.get("status", {}).get("conditions") or []]


def summarize_event(e: dict) -> dict:
    io = e.get("involvedObject") or {}
    return {
        "type": e.get("type"),
        "reason": e.get("reason"),
        "object": f"{io.get('kind')}/{io.get('name')}",
        "message": redact((e.get("message") or "")[:500]),
        "count": e.get("count") or (e.get("series") or {}).get("count") or 1,
        "firstSeen": e.get("firstTimestamp") or e.get("eventTime"),
        "lastSeen": e.get("lastTimestamp") or (e.get("series") or {}).get("lastObservedTime") or e.get("eventTime"),
    }


def summarize_workload(kind: str, w: dict) -> dict:
    meta, spec, st = w.get("metadata", {}), w.get("spec", {}), w.get("status", {})
    conds = [{"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason"), "message": (c.get("message") or "")[:200]}
             for c in st.get("conditions") or []]
    if kind == "DaemonSet":
        desired, ready = st.get("desiredNumberScheduled", 0), st.get("numberReady", 0)
    else:
        desired, ready = spec.get("replicas", 0), st.get("readyReplicas", 0)
    return {
        "kind": kind,
        "name": meta.get("name"),
        "desired": desired,
        "ready": ready or 0,
        "updated": st.get("updatedReplicas"),
        "available": st.get("availableReplicas"),
        "generation": meta.get("generation"),
        "observedGeneration": st.get("observedGeneration"),
        "revision": (meta.get("annotations") or {}).get("deployment.kubernetes.io/revision"),
        "images": [c.get("image") for c in spec.get("template", {}).get("spec", {}).get("containers", [])],
        "selector": spec.get("selector", {}).get("matchLabels") or {},
        "conditions": conds,
        "age": _age(meta.get("creationTimestamp")),
    }


def _near_miss(selector: dict[str, str], pods: list[dict]) -> list[dict]:
    """Pods that share every selector key but differ in some values: likely the intended target."""
    out = []
    for p in pods:
        labels = p["metadata"].get("labels") or {}
        if all(k in labels for k in selector) and any(labels[k] != v for k, v in selector.items()):
            out.append({"pod": p["metadata"]["name"], "labels": {k: labels[k] for k in selector}})
    return out[:5]


def eks_endpoint_and_token(cluster: str, region: str, role_arn: str | None = None, session=None) -> tuple[dict, str]:
    """Return ({endpoint, ca}, token) for an EKS cluster using the AWS default credential chain."""
    import base64

    import boto3
    from botocore.signers import RequestSigner

    session = session or boto3.session.Session(region_name=region)
    if role_arn:
        c = session.client("sts").assume_role(RoleArn=role_arn, RoleSessionName="moks-eks", DurationSeconds=3600)["Credentials"]
        session = boto3.session.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                                        aws_session_token=c["SessionToken"], region_name=region)
    try:
        desc = session.client("eks", region_name=region).describe_cluster(name=cluster)["cluster"]
        info = {"endpoint": desc["endpoint"], "ca": desc["certificateAuthority"]["data"]}
    except Exception as exc:
        raise ConnectorError("unavailable", f"eks:DescribeCluster {cluster} failed ({type(exc).__name__})") from None
    return info, eks_token(session, cluster, region)


def eks_token(session, cluster: str, region: str) -> str:
    import base64

    from botocore.signers import RequestSigner

    sts = session.client("sts", region_name=region)
    signer = RequestSigner(sts.meta.service_model.service_id, region, "sts", "v4", session.get_credentials(), session.events)
    url = signer.generate_presigned_url(
        {"method": "GET", "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
         "body": {}, "headers": {"x-k8s-aws-id": cluster}, "context": {}},
        region_name=region, expires_in=60, operation_name="")
    return "k8s-aws-v1." + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def eks_control_plane(cluster: str, region: str, role_arn: str | None = None, session=None) -> dict:
    """Read-only EKS control-plane view: cluster, node groups, add-ons, Fargate profiles."""
    import boto3

    session = session or boto3.session.Session(region_name=region)
    if role_arn:
        c = session.client("sts").assume_role(RoleArn=role_arn, RoleSessionName="moks-eks-read", DurationSeconds=900)["Credentials"]
        session = boto3.session.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                                        aws_session_token=c["SessionToken"], region_name=region)
    eks = session.client("eks", region_name=region)
    try:
        cl = eks.describe_cluster(name=cluster)["cluster"]
        ngs = [eks.describe_nodegroup(clusterName=cluster, nodegroupName=n)["nodegroup"] for n in eks.list_nodegroups(clusterName=cluster)["nodegroups"][:30]]
        addons = [eks.describe_addon(clusterName=cluster, addonName=a)["addon"] for a in eks.list_addons(clusterName=cluster)["addons"][:40]]
        fargate = eks.list_fargate_profiles(clusterName=cluster).get("fargateProfileNames", [])
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
        raise ConnectorError("forbidden" if "AccessDenied" in code else "unavailable", f"EKS API {code} for cluster {cluster}") from None
    return {
        "cluster": {"name": cl["name"], "version": cl.get("version"), "status": cl.get("status"), "platformVersion": cl.get("platformVersion"),
                    "endpointPublic": (cl.get("resourcesVpcConfig") or {}).get("endpointPublicAccess"),
                    "endpointPrivate": (cl.get("resourcesVpcConfig") or {}).get("endpointPrivateAccess"),
                    "logging": [t for g in (cl.get("logging") or {}).get("clusterLogging", []) if g.get("enabled") for t in g.get("types", [])],
                    "health_issues": (cl.get("health") or {}).get("issues", [])},
        "nodegroups": [{"name": n["nodegroupName"], "status": n.get("status"), "version": n.get("version"), "releaseVersion": n.get("releaseVersion"),
                        "capacityType": n.get("capacityType"), "instanceTypes": n.get("instanceTypes"), "scaling": n.get("scalingConfig"),
                        "health_issues": [{"code": i.get("code"), "message": i.get("message")} for i in (n.get("health") or {}).get("issues", [])]}
                       for n in ngs],
        "addons": [{"name": a["addonName"], "version": a.get("addonVersion"), "status": a.get("status"),
                    "health_issues": [{"code": i.get("code"), "message": i.get("message")} for i in (a.get("health") or {}).get("issues", [])]}
                   for a in addons],
        "fargate_profiles": fargate,
    }
