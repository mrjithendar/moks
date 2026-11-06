"""The crew's tools: an allowlist of read-only MOKS tools bound to one target.

The model chooses a tool and a few narrow arguments (a pod, a container, a search phrase). The target,
meaning the person, environment, namespace, workload and time window, is bound here from the
investigation and never taken from the model. A namespace or pod outside the target is refused here and
audited. Everything that is allowed still goes through Investigator._call and the ToolExecutor, so
authorization, the request's scope, timeouts, redaction, truncation, evidence and audit are the same
checks every MOKS tool call passes. There is no exec, apply, patch, delete, scale or Secret read: those
tools are not on the list, and the executor refuses privileged tools whatever asks.
"""
from __future__ import annotations

import json
import re
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


Binder = Callable[[Target, Any], dict[str, Any]]


@dataclass(frozen=True)
class CrewTool:
    name: str
    tool: str  # the MOKS tool it runs
    agent: str
    description: str
    args: type[Narrow]
    bind: Binder
    needs: str  # connector type, for the gap a missing source leaves


def _pod(t: Target, pod: str) -> str:
    """Pods of a Deployment are <name>-<hash>-<id>, of a StatefulSet <name>-<n>, of a DaemonSet <name>-<id>."""
    if not pod.startswith(f"{t.workload}-"):
        raise Refused(f"pod {pod} does not belong to {t.kind.lower()} {t.workload}")
    return pod


def _object(t: Target, name: str | None) -> str | None:
    if name and name != t.workload:
        _pod(t, name)
    return name


def _repository(t: Target) -> str:
    if not t.repository:
        raise LookupError("git: no single repository is configured for this environment, so commits cannot be tied to the workload")
    return t.repository


def _pods(t: Target) -> str:
    return f"{re.escape(t.workload)}-.*"


TOOLS: tuple[CrewTool, ...] = (
    CrewTool("namespace_health", "k8s_triage", "workload", "Health check of the target namespace with automatic findings (pods, warning events, services, workloads). Start here.",
             NoArgs, lambda t, a: {"namespace": t.namespace}, "kubernetes"),
    CrewTool("workload_status", "k8s_workloads", "workload", "Replica health, rollout conditions and images of the target workload.",
             NoArgs, lambda t, a: {"namespace": t.namespace}, "kubernetes"),
    CrewTool("workload_pods", "k8s_list_pods", "workload", "The target workload's pods: status, readiness, restarts and problem reasons.",
             NoArgs, lambda t, a: {"namespace": t.namespace}, "kubernetes"),
    CrewTool("describe_pod", "k8s_describe_pod", "workload", "One pod of the target workload: containers, images, probes, state, last state, conditions, events.",
             PodArgs, lambda t, a: {"namespace": t.namespace, "pod": _pod(t, a.pod)}, "kubernetes"),
    CrewTool("recent_events", "k8s_events", "workload", "Kubernetes events (warnings and normal) for the workload, one of its pods, or the namespace.",
             EventArgs, lambda t, a: {"namespace": t.namespace, "object_name": _object(t, a.object_name), "warnings_only": False}, "kubernetes"),
    CrewTool("pod_logs", "k8s_pod_logs", "workload", "The tail of one pod's container logs, optionally from the previous crashed instance.",
             LogArgs, lambda t, a: {"namespace": t.namespace, "pod": _pod(t, a.pod), "container": a.container, "previous": a.previous,
                                    "tail_lines": 0, "since_minutes": t.minutes}, "kubernetes"),
    CrewTool("search_logs", "loki_search", "workload", "Search the workload's logs in Loki over the investigation window.",
             SearchArgs, lambda t, a: {"namespace": t.namespace, "pod": _pods(t), "contains": a.contains, "errors_only": a.errors_only, "limit": 0, "minutes": t.minutes}, "loki"),
    CrewTool("pod_metrics", "prom_pod_resources", "workload", "CPU, memory, memory limit and throttling of the workload's pods (Prometheus).",
             NoArgs, lambda t, a: {"namespace": t.namespace, "pod_regex": _pods(t), "minutes": max(5, t.minutes)}, "prometheus"),
    CrewTool("firing_alerts", "prom_alerts", "workload", "Prometheus alerts firing within your scope.",
             NoArgs, lambda t, a: {"firing_only": True}, "prometheus"),
    CrewTool("rollout_history", "k8s_rollout_history", "change", "Revisions of the target Deployment with images and creation times.",
             NoArgs, lambda t, a: {"namespace": t.namespace, "deployment": t.workload}, "kubernetes"),
    CrewTool("config_audit", "k8s_config_audit", "change", "Configuration of the namespace's workloads: requests, probes, image pinning, missing Secret/ConfigMap references (contents never read).",
             NoArgs, lambda t, a: {"namespace": t.namespace}, "kubernetes"),
    CrewTool("recent_commits", "git_recent_commits", "change", "Commits in the environment's repository during the investigation window.",
             PathArgs, lambda t, a: {"repository": _repository(t), "path": a.path, "hours": max(1, -(-t.minutes // 60)), "limit": 20}, "git"),
    CrewTool("deployed_commit", "git_compare_deployment", "change", "Ties the running Deployment to its Git commit when its image or annotations carry a SHA.",
             NoArgs, lambda t, a: {"namespace": t.namespace, "deployment": t.workload, "repository": _repository(t)}, "git"),
    CrewTool("search_runbooks", "kb_search", "change", "Search runbooks, docs and approved postmortems you may read (cited passages).",
             QueryArgs, lambda t, a: {"query": a.query, "k": 4}, "knowledge"),
)
BY_NAME = {t.name: t for t in TOOLS}
ALLOWED_MOKS_TOOLS = frozenset(t.tool for t in TOOLS)


def bind(tool: CrewTool, target: Target, raw: dict[str, Any], *, log_lines: int) -> dict[str, Any]:
    """The MOKS arguments for a crew call. Raises Refused for anything outside the target, LookupError
    for a source that is not configured, and pydantic's ValidationError for malformed arguments."""
    parsed = tool.args.model_validate(raw)
    if parsed.namespace and parsed.namespace != target.namespace:
        raise Refused(f"namespace {parsed.namespace} is outside this investigation (namespace {target.namespace})")
    args = tool.bind(target, parsed)
    if "tail_lines" in args:
        args["tail_lines"] = log_lines
    if args.get("limit") == 0:
        args["limit"] = log_lines
    return {k: v for k, v in args.items() if v is not None}


def _about(target: Target, item: Any) -> bool:
    return f'"{target.workload}"' in (body := json.dumps(item, default=str)) or f'"{target.workload}-' in body or f"/{target.workload}" in body


def focus(target: Target, tool: str, data: Any) -> Any:
    """The target's part of a namespace-wide result, for the excerpt. None means use the whole result.
    The stored evidence row keeps everything; this only decides what the model is shown."""
    if tool in ("k8s_workloads", "k8s_list_pods") and isinstance(data, list):
        return [x for x in data if _about(target, x)] or None
    if tool == "k8s_config_audit" and isinstance(data, dict):
        return {**data, "workloads": [w for w in data.get("workloads") or [] if _about(target, w)]}
    if tool == "k8s_triage" and isinstance(data, dict):
        return {k: [x for x in data.get(k) or [] if _about(target, x)] for k in ("findings", "unhealthyPods", "workloads", "warningEvents")}
    return None


def mine(target: Target, finding: dict[str, Any]) -> bool:
    return any(str(r).rsplit("/", 1)[-1] == target.workload or str(r).rsplit("/", 1)[-1].startswith(f"{target.workload}-") for r in finding.get("resources") or [])
