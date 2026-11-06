"""Cluster state for the portal's browser tests and screenshots: a fixture, not mk4.

Bigger than tests/fakes.py on purpose: 220 pods in one namespace (some with 63-character names), mk4's real
kindnet DaemonSet resources in kube-system, five nodes, and Prometheus answers for the FinOps usage queries.
frontend/e2e/fixture_server.py serves the real API with these connectors, and the API tests use them too.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

from moks.connectors.base import ConnectorError
from moks.connectors.kubernetes import summarize_pod
from tests.fakes import FakeKubernetes, ago

Mi = 1024**2
LONG = "payments-reconciliation-ledger-writer-eu-central-1-7d9f8c6b5-x2k4q"  # 67 characters, like a real ReplicaSet pod
assert len(LONG) >= 63


def _pod(name: str, ns: str, *, owner: tuple[str, str], container: str = "app", requests: dict | None = None, limits: dict | None = None,
         waiting: str | None = None, last: str | None = None, restarts: int = 0, node: str = "mk4-m02", labels: dict | None = None) -> dict:
    cs: dict[str, Any] = {"name": container, "ready": waiting is None, "restartCount": restarts, "state": {"running": {"startedAt": ago(300)}}}
    if waiting:
        cs["state"] = {"waiting": {"reason": waiting, "message": f"{waiting}: back-off restarting failed container"}}
    if last:
        cs["lastState"] = {"terminated": {"reason": last, "exitCode": 137 if last == "OOMKilled" else 1, "finishedAt": ago(7)}}
    return {"metadata": {"name": name, "namespace": ns, "labels": labels or {"app": owner[1]}, "creationTimestamp": ago(60 * 24),
                         "ownerReferences": [{"kind": owner[0], "name": owner[1]}]},
            "spec": {"nodeName": node, "containers": [{"name": container, "image": f"ghcr.io/example/{owner[1]}:1.4.2", "ports": [{"name": "http", "containerPort": 8080}],
                                                       "resources": {"requests": requests or {"cpu": "100m", "memory": "128Mi"}, "limits": limits or {"memory": "256Mi"}}, "env": []}]},
            "status": {"phase": "Running", "containerStatuses": [cs], "conditions": [{"type": "Ready", "status": "True" if waiting is None else "False"}]}}


def _shop() -> list[dict]:
    pods = [_pod(LONG, "shop", owner=("ReplicaSet", "payments-reconciliation-ledger-writer-eu-central-1-7d9f8c6b5"), waiting="CrashLoopBackOff", last="Error", restarts=14),
            _pod("cart-redis-cache-0", "shop", owner=("StatefulSet", "cart-redis-cache"), last="OOMKilled", restarts=3)]
    apps = ["catalogue", "checkout", "shipping", "ratings", "user", "web", "dispatch", "payment", "inventory-sync-worker-with-a-long-name"]
    for i in range(218):
        app = apps[i % len(apps)]
        pods.append(_pod(f"{app}-{5 + i % 7}d{i % 10}f{i % 9}c-{i:05d}", "shop", owner=("ReplicaSet", f"{app}-{5 + i % 7}d{i % 10}f{i % 9}c"), node=f"mk4-m0{1 + i % 5}",
                         restarts=1 if i % 37 == 0 else 0))
    return pods


KINDNET = [_pod(f"kindnet-{s}", "kube-system", owner=("DaemonSet", "kindnet"), container="kindnet-cni", node=f"mk4-m0{i + 1}",
                requests={"cpu": "100m", "memory": "50Mi"}, limits={"cpu": "100m", "memory": "50Mi"}, labels={"app": "kindnet"})
           for i, s in enumerate(["4xq2z", "8kd7p", "c2m9w", "h6t3r", "w9b5n"])]

SCENARIO = {
    "shop": {"pods": _shop(), "services": [{"name": "payments", "selector": {"app": "payments-reconciliation-ledger-writer-eu-central-1-7d9f8c6b5"}}],
             "deployments": [{"name": "checkout", "replicas": 3, "ready": 3}, {"name": "payments-reconciliation-ledger-writer-eu-central-1", "replicas": 1, "ready": 0}],
             "logs": 'level=error msg="ledger write failed" err="connection refused" upstream=postgres:5432\nlevel=info msg="retrying in 5s"\n'},
    "kube-system": {"pods": KINDNET, "services": [], "deployments": [], "logs": "kindnet: route sync ok\n"},
    "empty-ns": {"pods": [], "services": [], "deployments": [], "logs": ""},
    # 120 job pods with 63+ character names: with shop, "all namespaces" is over 200 rows
    "batch": {"pods": [_pod(f"nightly-ledger-reconciliation-export-to-warehouse-job-2026110{i % 10}-{i:05d}", "batch", owner=("Job", f"nightly-ledger-export-{i:03d}"), node=f"mk4-m0{1 + i % 5}")
                       for i in range(120)], "services": [], "deployments": [], "logs": "export ok\n"},
}


class PortalKubernetes(FakeKubernetes):
    def _ns(self, ns: str) -> dict:
        if ns not in SCENARIO:
            raise ConnectorError("not_found", f"namespace {ns} not found")
        return SCENARIO[ns]

    async def list_namespaces(self):
        return sorted(SCENARIO)

    async def list_pods(self, namespace, label_selector=None):
        return [summarize_pod(p) for p in self._ns(namespace)["pods"]], False

    async def nodes(self):
        return [{"name": f"mk4-m0{i}", "ready": True, "pressure": [], "unschedulable": False, "taints": [], "allocatable": {"cpu": "4", "memory": "8Gi", "pods": "110"}}
                for i in range(1, 6)]

    async def events(self, namespace, involved_name=None, involved_kind=None, warnings_only=True, limit=100):
        self._ns(namespace)
        if namespace != "shop":
            return []
        return [{"type": "Warning", "reason": "BackOff", "object": f"Pod/{LONG}", "message": "Back-off restarting failed container ledger-writer",
                 "count": 14, "firstSeen": ago(50), "lastSeen": ago(1)},
                {"type": "Warning", "reason": "OOMKilling", "object": "Pod/cart-redis-cache-0", "message": "Memory cgroup out of memory: Killed process 4121 (redis-server)",
                 "count": 3, "firstSeen": ago(40), "lastSeen": ago(7)}]


# ---- Prometheus ------------------------------------------------------------------------------------------------

USAGE = {  # (namespace, pod prefix, container) -> (cpu p95 cores, max working set bytes)
    ("kube-system", "kindnet-", "kindnet-cni"): (0.0012, 47 * Mi),  # near its 50Mi limit, almost no CPU: the reported row
    ("shop", "catalogue-", "app"): (0.004, 40 * Mi),
    ("shop", "checkout-", "app"): (0.18, 120 * Mi),
}


def _usage_for(ns: str, pod: str, container: str) -> tuple[float, float]:
    for (n, prefix, c), v in USAGE.items():
        if n == ns and pod.startswith(prefix) and c == container:
            return v
    return 0.05, 90 * Mi


class PortalPrometheus:
    type = "prometheus"
    public_url = None

    def __init__(self, config: dict | None = None, credential_ref: dict | None = None):
        pass

    async def aclose(self):
        pass

    async def test(self):
        return "fixture Prometheus"

    def graph_link(self, promql: str, minutes: int):
        return None

    def _namespaces(self, q: str) -> list[str]:
        m = re.search(r'namespace=~?"([^"]+)"', q)
        if not m:
            return list(SCENARIO)
        val = m.group(1)
        return [val] if val in SCENARIO else [n for n in SCENARIO if n.replace("-", "\\\\-") in val or n in val]

    async def instant(self, promql: str, at: datetime) -> dict[str, Any]:
        kind = next((k for k, pre in (("cpu", "rate(container_cpu_usage_seconds_total"), ("samples", "count_over_time"), ("mem", "container_memory_working_set_bytes")) if pre in promql), None)
        series = []
        if kind and "by (namespace, pod, container)" in promql:
            for ns in self._namespaces(promql):
                for p in SCENARIO[ns]["pods"]:
                    c = p["spec"]["containers"][0]["name"]
                    cpu, mem = _usage_for(ns, p["metadata"]["name"], c)
                    v = {"cpu": cpu * (0.5 if promql.startswith("avg_over_time") else 1), "mem": mem * (0.8 if promql.startswith("avg_over_time") else 1), "samples": 672.0}[kind]
                    series.append({"metric": {"namespace": ns, "pod": p["metadata"]["name"], "container": c}, "value": [at.timestamp(), v]})
        return {"resultType": "vector", "series": series[:25], "all_series": series, "warnings": []}

    async def range(self, promql: str, start: datetime, end: datetime, step_seconds: int) -> dict[str, Any]:
        t, vals = start.timestamp(), []
        while t <= end.timestamp():
            vals.append([t, round(0.2 + 0.1 * math.sin(t / 900), 4)])
            t += step_seconds
        return {"resultType": "matrix", "series": [{"metric": {"namespace": "shop", "pod": "checkout-5d0f0c-00001"}, "values": vals}], "warnings": []}

    async def label_names(self, match, start, end):
        return ["container", "namespace", "pod"]

    async def label_values(self, label, match=None, start=None, end=None, limit=500):
        if label == "__name__":
            return ["container_cpu_usage_seconds_total", "container_memory_working_set_bytes", "http_request_duration_seconds_bucket", "http_requests_total",
                    "kube_pod_container_status_restarts_total"]
        return {"namespace": sorted(SCENARIO), "pod": [p["metadata"]["name"] for p in SCENARIO["shop"]["pods"][:30]], "container": ["app", "kindnet-cni"]}.get(label, [])

    async def metadata(self):
        return {"container_cpu_usage_seconds_total": {"type": "counter", "unit": "", "help": "Cumulative cpu time consumed"},
                "container_memory_working_set_bytes": {"type": "gauge", "unit": "", "help": "Current working set"}}

    async def metric_names(self, *a, **k):
        return list((await self.metadata()))

    async def active_alerts(self):
        return [{"labels": {"alertname": "KubePodCrashLooping", "namespace": "shop", "severity": "warning", "pod": LONG}, "annotations": {"summary": "Pod is crash looping"},
                 "state": "firing", "activeAt": datetime.now(timezone.utc).isoformat()}]

    async def alerts(self):
        return [{"group": "kubernetes-apps", "name": "KubePodCrashLooping", "state": "firing", "query": "rate(kube_pod_container_status_restarts_total[10m]) > 0",
                 "for": 900, "labels": {"severity": "warning"}, "annotations": {"summary": "Pod is crash looping"},
                 "active": [{"labels": {"alertname": "KubePodCrashLooping", "namespace": "shop", "pod": LONG}, "state": "firing", "activeAt": datetime.now(timezone.utc).isoformat(), "value": "1"}]}]

    async def targets(self):
        return []


class PortalLoki:
    type = "loki"
    namespace_label = "namespace"

    def __init__(self, config: dict | None = None, credential_ref: dict | None = None):
        pass

    async def aclose(self):
        pass

    async def test(self):
        return "fixture Loki"

    async def query_range(self, logql: str, start: datetime, end: datetime, limit: int = 200, direction: str = "backward", **_: Any) -> dict[str, Any]:
        lines = []
        for i in range(min(limit, 60)):
            lvl = "error" if i % 6 == 0 else "info"
            lines.append({"ts": ago(i), "line": f'level={lvl} msg="ledger write {"failed" if lvl == "error" else "ok"}" request_id=req-{i:04d} upstream=postgres:5432',
                          "labels": {"namespace": "shop", "pod": LONG, "container": "app", "level": lvl}})
        return {"resultType": "streams", "lines": lines, "stats": {}}

    async def labels(self, *a, **k):
        return ["namespace", "pod", "container", "level"]

    async def label_values(self, label, *a, **k):
        return sorted(SCENARIO) if label == "namespace" else []


FIXTURE_TYPES = {"kubernetes": PortalKubernetes, "prometheus": PortalPrometheus, "loki": PortalLoki}
