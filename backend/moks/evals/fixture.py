"""Fixture Kubernetes connector for evaluation: serves recorded cluster state from a dataset case
through the same interface (and summarizers) as the live connector."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..connectors.base import ConnectorError
from ..connectors.kubernetes import describe_containers, summarize_pod, summarize_workload


def _ago(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_pod(spec: dict[str, Any], ns: str) -> dict:
    cs: dict[str, Any] = {"name": "app", "ready": spec.get("ready", True), "restartCount": spec.get("restarts", 0), "state": {"running": {}}}
    if spec.get("waiting"):
        cs["state"] = {"waiting": {"reason": spec["waiting"], "message": spec.get("message", "")}}
        cs["ready"] = False
    if spec.get("last"):
        cs["lastState"] = {"terminated": {"reason": spec["last"], "exitCode": spec.get("exit_code", 137 if spec["last"] == "OOMKilled" else 1),
                                          "finishedAt": _ago(spec.get("last_minutes_ago", 5))}}
    return {
        "metadata": {"name": spec["name"], "namespace": ns, "labels": spec.get("labels", {}), "creationTimestamp": _ago(600),
                     "ownerReferences": [{"kind": "ReplicaSet", "name": spec["name"].rsplit("-", 1)[0]}]},
        "spec": {"nodeName": "node-1", "containers": [{"name": "app", "image": spec.get("image", "example/app:1"),
                                                       "ports": [{"name": "http", "containerPort": 8080}],
                                                       "resources": spec.get("resources", {}), "env": spec.get("env", [])}]},
        "status": {"phase": spec.get("phase", "Running"), "containerStatuses": [cs], "conditions": []},
    }


class FixtureKubernetes:
    type = "kubernetes"

    def __init__(self, case: dict[str, Any]):
        self.ns = case["namespace"]
        f = case["fixture"]
        self.pods = [build_pod(p, self.ns) for p in f.get("pods", [])]
        self.services_def = f.get("services", [])
        self.deployments = f.get("deployments", [])
        self.logs = f.get("logs", {})
        self.events_def = f.get("events", [])
        self.rollouts = f.get("rollouts", {})

    async def aclose(self):
        pass

    def _check(self, ns: str):
        if ns != self.ns:
            raise ConnectorError("not_found", f"namespace {ns} not found")

    async def list_namespaces(self):
        return [self.ns]

    async def list_pods(self, namespace, label_selector=None):
        self._check(namespace)
        return [summarize_pod(p) for p in self.pods], False

    async def raw_pods(self, namespace):
        self._check(namespace)
        return self.pods

    async def describe_pod(self, namespace, name):
        self._check(namespace)
        p = next((p for p in self.pods if p["metadata"]["name"] == name), None)
        if not p:
            raise ConnectorError("not_found", f"pod {name} not found")
        return {"summary": summarize_pod(p), "containers": describe_containers(p), "conditions": [], "node": "node-1", "events": []}

    async def pod_logs(self, namespace, pod, container, previous, tail_lines, since_seconds):
        self._check(namespace)
        return self.logs.get(pod, self.logs.get("*", ""))

    async def events(self, namespace, involved_name=None, involved_kind=None, warnings_only=True, limit=100):
        self._check(namespace)
        return [{"type": e.get("type", "Warning"), "reason": e["reason"], "object": e["object"], "message": e.get("message", ""),
                 "count": e.get("count", 1), "firstSeen": None, "lastSeen": _ago(e.get("minutes_ago", 2))} for e in self.events_def]

    async def workloads(self, namespace):
        self._check(namespace)
        return [summarize_workload("Deployment", {
            "metadata": {"name": d["name"], "generation": 2, "annotations": {"deployment.kubernetes.io/revision": str(d.get("revision", 1))}},
            "spec": {"replicas": d["replicas"], "selector": {"matchLabels": {"app": d["name"]}},
                     "template": {"spec": {"containers": [{"image": d.get("image", "example/app:1")}]}}},
            "status": {"readyReplicas": d["ready"], "observedGeneration": 2}}) for d in self.deployments]

    async def rollout_history(self, namespace, deployment):
        self._check(namespace)
        revs = self.rollouts.get(deployment, [{"revision": 1, "minutes_ago": 3000, "images": ["example/app:1"]}])
        return {"deployment": {"name": deployment}, "revisions": [
            {"revision": r["revision"], "created": _ago(r["minutes_ago"]), "images": r["images"], "replicaSet": f"{deployment}-{r['revision']}"}
            for r in sorted(revs, key=lambda r: -r["revision"])]}

    async def services(self, namespace):
        self._check(namespace)
        out = []
        for s in self.services_def:
            matching = [p for p in self.pods if all(p["metadata"]["labels"].get(k) == v for k, v in s["selector"].items())]
            near = [{"pod": p["metadata"]["name"], "labels": {k: p["metadata"]["labels"][k] for k in s["selector"]}}
                    for p in self.pods if not matching and all(k in p["metadata"]["labels"] for k in s["selector"])]
            ready = sum(1 for p in matching if p["status"]["containerStatuses"][0]["ready"])
            out.append({"name": s["name"], "type": "ClusterIP", "selector": s["selector"], "ports": [{"name": "http", "port": 80, "targetPort": "http"}],
                        "endpoints": {"ready": ready, "notReady": len(matching) - ready}, "matchingPods": [p["metadata"]["name"] for p in matching],
                        "matchingPodCount": len(matching), "podContainerPorts": [("http", 8080)] if matching else [], "nearMissPods": near})
        return out

    async def ingresses(self, namespace):
        return []

    async def nodes(self):
        return [{"name": "node-1", "ready": True, "pressure": [], "unschedulable": False, "taints": [], "allocatable": {"cpu": "4", "memory": "16Gi"}}]

    async def dns_health(self):
        return {"deployment": {}, "services": [], "pods": []}
