"""Kubernetes connector (Minikube in Phase 1; EKS uses the same code with an exec/IRSA kubeconfig).

Read-only by construction: this class has no methods that create, patch or delete, and none
that read Secrets. Its in-cluster ServiceAccount is bound to a ClusterRole without Secret
access (deploy/helm/moks/templates/rbac.yaml), so a bug here still cannot read them.

Results are compact summaries (not raw objects) so evidence stays small and explainable.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any

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


class KubernetesConnector:
    type = "kubernetes"

    def __init__(self, config: dict[str, Any], credential_ref: dict[str, Any] | None = None):
        self.config = config
        self.credential_ref = credential_ref
        self.timeout = float(config.get("timeout_seconds", 15))
        self._api = None
        self._lock = asyncio.Lock()
        self._api_groups: set[str] | None = None

    async def _client(self):
        async with self._lock:
            if self._api is not None:
                return self._api
            from kubernetes_asyncio import client, config as kconfig

            cfg = client.Configuration()
            mode = self.config.get("mode", "in_cluster")
            try:
                if mode == "in_cluster":
                    kconfig.load_incluster_config(client_configuration=cfg)
                elif mode == "kubeconfig":
                    if not self.credential_ref or self.credential_ref.get("type") != "file":
                        raise ConnectorError("config", "kubeconfig mode needs a file credential reference to the kubeconfig")
                    path = resolve_credential_path(self.credential_ref)
                    await kconfig.load_kube_config(config_file=str(path), context=self.config.get("context") or None,
                                                   client_configuration=cfg)
                else:
                    raise ConnectorError("config", f"unknown kubernetes mode {mode!r} (in_cluster | kubeconfig)")
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
