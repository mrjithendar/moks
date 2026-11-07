"""The crew's tools: an allowlist of read-only MOKS tools bound to one target.

The model chooses a tool and a few narrow arguments (a pod, a container, a search phrase). The target,
meaning the person, environment, namespace, workload and time window, is bound here from the
investigation and never taken from the model. A namespace or pod outside the target is refused here and
audited. Everything that is allowed still goes through Investigator._call and the ToolExecutor, so
authorization, the request's scope, timeouts, redaction, truncation, evidence and audit are the same
checks every MOKS tool call passes. There is no exec, apply, patch, delete, scale or Secret read: those
tools are not on the list, and the executor refuses privileged tools whatever asks.

Which pods belong to the workload is decided by Kubernetes owner references, never by name: a Deployment's
pods are those whose controller is one of the ReplicaSets that Deployment (by UID) owns; a StatefulSet's or
DaemonSet's are those it controls directly, by UID. Members holds the result (runner.Run resolves it through
the ToolExecutor). So cart's investigation never reads cart-worker's pods, a pod without owner references
belongs to nothing, and a recreated workload's old pods drop out. When nothing matches, the answer is an
explicit empty result, never the whole namespace.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..tools.contracts import K8S_NAME

NAMESPACE_HINT = "Leave empty. The namespace is fixed by MOKS; any other value is refused."


class Narrow(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    namespace: str | None = Field(default=None, max_length=253, description=NAMESPACE_HINT)


class NoArgs(Narrow):
    pass


class PodArgs(Narrow):
    pod: str = Field(pattern=K8S_NAME, max_length=253, description="a pod of the target workload, as listed by workload_pods")


class LogArgs(PodArgs):
    container: str | None = Field(default=None, pattern=K8S_NAME, max_length=63)
    previous: bool = Field(default=False, description="logs of the previous (crashed) container instance")


class EventArgs(Narrow):
    object_name: str | None = Field(default=None, pattern=K8S_NAME, max_length=253, description="the workload or one of its pods; empty for the namespace")


class SearchArgs(Narrow):
    contains: str | None = Field(default=None, max_length=200, description="text to find in the workload's logs")
    errors_only: bool = False


class PathArgs(Narrow):
    path: str | None = Field(default=None, max_length=300, pattern=r"^[A-Za-z0-9_./-]{1,300}$", description="limit to commits touching this path")


class QueryArgs(Narrow):
    query: str = Field(min_length=2, max_length=300, description="what to look for in runbooks and docs")


@dataclass(frozen=True)
class Target:
    environment: str
    namespace: str
    workload: str
    kind: str
    minutes: int
    repository: str  # "" when no single repository is configured


class Refused(Exception):
    """A call outside the target. Audited by the caller, never run."""


class Empty(Exception):
    """The workload has no pods to read for this call: answered as an explicit empty result, never run wider."""


@dataclass(frozen=True)
class Members:
    """The target's verified pods: owners are (kind, name, uid) of the controllers that own them (the Deployment's
    ReplicaSets, or the StatefulSet or DaemonSet itself)."""
    pods: frozenset[str]
    owners: frozenset[tuple[str, str, str | None]]
    reason: str = ""  # why there are none, when there are none

    def regex(self) -> str:
        """An exact alternation of the member pod names, for Loki and Prometheus pod matchers."""
        return "(" + "|".join(sorted(p.replace(".", "[.]") for p in self.pods)) + ")"


NOBODY = Members(frozenset(), frozenset(), "the workload's pods have not been resolved")


def owned_by(pod: dict[str, Any], owners: frozenset[tuple[str, str, str | None]]) -> bool:
    """A pod summary (summarize_pod) belongs to the target when its controller is one of the owners, matched by UID
    whenever the owner's UID is known. No owner reference, no membership."""
    kind, _, name = str(pod.get("owner") or "").partition("/")
    return any(kind == k and name == n and (uid is None or pod.get("ownerUid") == uid) for k, n, uid in owners)


Binder = Callable[[Target, Any, Members], dict[str, Any]]


@dataclass(frozen=True)
class CrewTool:
    name: str
    tool: str  # the MOKS tool it runs
    agent: str
    description: str
    args: type[Narrow]
    bind: Binder
    needs: str  # connector type, for the gap a missing source leaves


def _pod(t: Target, m: Members, pod: str) -> str:
    if pod not in m.pods:
        raise Refused(f"pod {pod} is not owned by {t.kind.lower()} {t.workload} (by its owner references)")
    return pod


def _object(t: Target, m: Members, name: str | None) -> str | None:
    if name and name != t.workload:
        _pod(t, m, name)
    return name


def _repository(t: Target) -> str:
    if not t.repository:
        raise LookupError("git: no single repository is configured for this environment, so commits cannot be tied to the workload")
    return t.repository


def _pods(t: Target, m: Members) -> str:
    if not m.pods:
        raise Empty(f"no pods are owned by {t.kind.lower()} {t.workload}" + (f": {m.reason}" if m.reason else ""))
    return m.regex()


NEEDS_MEMBERS = frozenset({"describe_pod", "pod_logs", "recent_events", "search_logs", "pod_metrics", "workload_pods", "namespace_health"})

TOOLS: tuple[CrewTool, ...] = (
    CrewTool("namespace_health", "k8s_triage", "workload", "Health check of the target namespace with automatic findings (pods, warning events, services, workloads). Start here.",
             NoArgs, lambda t, a, m: {"namespace": t.namespace}, "kubernetes"),
    CrewTool("workload_status", "k8s_workloads", "workload", "Replica health, rollout conditions and images of the target workload.",
             NoArgs, lambda t, a, m: {"namespace": t.namespace}, "kubernetes"),
    CrewTool("workload_pods", "k8s_list_pods", "workload", "The target workload's pods: status, readiness, restarts and problem reasons.",
             NoArgs, lambda t, a, m: {"namespace": t.namespace}, "kubernetes"),
    CrewTool("describe_pod", "k8s_describe_pod", "workload", "One pod of the target workload: containers, images, probes, state, last state, conditions, events.",
             PodArgs, lambda t, a, m: {"namespace": t.namespace, "pod": _pod(t, m, a.pod)}, "kubernetes"),
    CrewTool("recent_events", "k8s_events", "workload", "Kubernetes events (warnings and normal) for the workload, one of its pods, or the namespace.",
             EventArgs, lambda t, a, m: {"namespace": t.namespace, "object_name": _object(t, m, a.object_name), "warnings_only": False}, "kubernetes"),
    CrewTool("pod_logs", "k8s_pod_logs", "workload", "The tail of one pod's container logs, optionally from the previous crashed instance.",
             LogArgs, lambda t, a, m: {"namespace": t.namespace, "pod": _pod(t, m, a.pod), "container": a.container, "previous": a.previous,
                                    "tail_lines": 0, "since_minutes": t.minutes}, "kubernetes"),
    CrewTool("search_logs", "loki_search", "workload", "Search the workload's logs in Loki over the investigation window.",
             SearchArgs, lambda t, a, m: {"namespace": t.namespace, "pod": _pods(t, m), "contains": a.contains, "errors_only": a.errors_only, "limit": 0, "minutes": t.minutes}, "loki"),
    CrewTool("pod_metrics", "prom_pod_resources", "workload", "CPU, memory, memory limit and throttling of the workload's pods (Prometheus).",
             NoArgs, lambda t, a, m: {"namespace": t.namespace, "pod_regex": _pods(t, m), "minutes": max(5, t.minutes)}, "prometheus"),
    CrewTool("firing_alerts", "prom_alerts", "workload", "Prometheus alerts firing within your scope.",
             NoArgs, lambda t, a, m: {"firing_only": True}, "prometheus"),
    CrewTool("rollout_history", "k8s_rollout_history", "change", "Revisions of the target Deployment with images and creation times.",
             NoArgs, lambda t, a, m: {"namespace": t.namespace, "deployment": t.workload}, "kubernetes"),
    CrewTool("config_audit", "k8s_config_audit", "change", "Configuration of the namespace's workloads: requests, probes, image pinning, missing Secret/ConfigMap references (contents never read).",
             NoArgs, lambda t, a, m: {"namespace": t.namespace}, "kubernetes"),
    CrewTool("recent_commits", "git_recent_commits", "change", "Commits in the environment's repository during the investigation window.",
             PathArgs, lambda t, a, m: {"repository": _repository(t), "path": a.path, "hours": max(1, -(-t.minutes // 60)), "limit": 20}, "git"),
    CrewTool("deployed_commit", "git_compare_deployment", "change", "Ties the running Deployment to its Git commit when its image or annotations carry a SHA.",
             NoArgs, lambda t, a, m: {"namespace": t.namespace, "deployment": t.workload, "repository": _repository(t)}, "git"),
    CrewTool("search_runbooks", "kb_search", "change", "Search runbooks, docs and approved postmortems you may read (cited passages).",
             QueryArgs, lambda t, a, m: {"query": a.query, "k": 4}, "knowledge"),
)
BY_NAME = {t.name: t for t in TOOLS}
ALLOWED_MOKS_TOOLS = frozenset(t.tool for t in TOOLS)


def bind(tool: CrewTool, target: Target, raw: dict[str, Any], *, log_lines: int, members: Members = NOBODY) -> dict[str, Any]:
    """The MOKS arguments for a crew call. Raises Refused for anything outside the target, Empty when the workload
    has no pods for it, LookupError for a source that is not configured, and pydantic's ValidationError for
    malformed arguments."""
    parsed = tool.args.model_validate(raw)
    if parsed.namespace and parsed.namespace != target.namespace:
        raise Refused(f"namespace {parsed.namespace} is outside this investigation (namespace {target.namespace})")
    args = tool.bind(target, parsed, members)
    if "tail_lines" in args:
        args["tail_lines"] = log_lines
    if args.get("limit") == 0:
        args["limit"] = log_lines
    return {k: v for k, v in args.items() if v is not None}


def _names(t: Target, m: Members) -> set[str]:
    """Every exact reference to the target: the workload, its controllers and its verified pods, as kind/name in
    the spellings MOKS results use (Pod/x and pod/x)."""
    refs = {f"{t.kind}/{t.workload}", f"{t.kind.lower()}/{t.workload}"}
    refs |= {f"{k}/{n}" for k, n, _ in m.owners} | {f"{k.lower()}/{n}" for k, n, _ in m.owners}
    return refs | {f"Pod/{p}" for p in m.pods} | {f"pod/{p}" for p in m.pods}


def _workload_row(t: Target, w: Any) -> bool:
    return isinstance(w, dict) and w.get("name") == t.workload and w.get("kind", t.kind) == t.kind


def focus(target: Target, tool: str, data: Any, members: Members = NOBODY) -> Any:
    """The target's part of a namespace-wide result, for the excerpt. None means the result is about the target
    already (one pod, one Deployment); a namespace-wide result is always narrowed, to an empty one when nothing in
    it is the target's. The stored evidence row keeps everything; this only decides what the model is shown."""
    if tool == "k8s_list_pods" and isinstance(data, list):
        return [p for p in data if isinstance(p, dict) and p.get("name") in members.pods]
    if tool == "k8s_workloads" and isinstance(data, list):
        return [w for w in data if _workload_row(target, w)]
    if tool == "k8s_config_audit" and isinstance(data, dict):
        return {**data, "workloads": [w for w in data.get("workloads") or [] if str(w.get("workload") or "") in _names(target, members)]}
    if tool == "k8s_triage" and isinstance(data, dict):
        refs = _names(target, members)
        return {"findings": [f for f in data.get("findings") or [] if mine(target, f, members)],
                "unhealthyPods": [p for p in data.get("unhealthyPods") or [] if p.get("name") in members.pods],
                "workloads": [w for w in data.get("workloads") or [] if _workload_row(target, w)],
                "warningEvents": [e for e in data.get("warningEvents") or [] if str(e.get("object") or "") in refs]}
    return None


def mine(target: Target, finding: dict[str, Any], members: Members = NOBODY) -> bool:
    refs = _names(target, members)
    return any(str(r) in refs for r in finding.get("resources") or []) or str(finding.get("controller") or finding.get("owner") or "") in refs
