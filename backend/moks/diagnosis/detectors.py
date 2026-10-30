"""Deterministic Kubernetes failure detectors.

They work on the compact summaries produced by the Kubernetes connector and return findings
that point to the evidence they came from. Findings are observations plus a suspected cause;
they never claim a verified root cause.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

IMAGE_PULL = {"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull", "RegistryUnavailable"}
CONFIG_ERRORS = {"CreateContainerConfigError", "CreateContainerError", "RunContainerError"}


def _finding(kind: str, severity: str, title: str, detail: str, resources: list[str], *,
             cause: str | None = None, next_checks: list[str] | None = None, remediation: str | None = None) -> dict[str, Any]:
    return {
        "kind": kind, "severity": severity, "title": title, "detail": detail, "resources": resources,
        "suspected_cause": cause, "next_checks": next_checks or [], "remediation_hint": remediation,
    }


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def in_window(value: str | None, since: datetime | None) -> bool:
    """True when the timestamp is inside the window (or there is no window)."""
    if since is None:
        return True
    t = _ts(value)
    return t is not None and t >= since


def is_unhealthy(p: dict, since: datetime | None = None) -> bool:
    """A pod needs attention when a container is waiting/failed/unschedulable now, a running
    pod is not ready, or a container restarted inside the window. Old restarts (for example
    after a node reboot days ago) do not count."""
    for pr in p.get("problems", []):
        if pr.get("state") in ("waiting", "terminated", "unschedulable"):
            return True
        if pr.get("state") == "lastTerminated" and in_window(pr.get("finishedAt"), since):
            return True
    ready, total = (p.get("ready") or "0/0").split("/")
    return p.get("phase") == "Running" and ready != total


def detect_pods(pods: list[dict], since: datetime | None = None) -> list[dict]:
    out = []
    restarted: list[tuple[dict, dict]] = []
    for p in pods:
        name = f"pod/{p['name']}"
        reasons = {(pr.get("state"), pr.get("reason")) for pr in p.get("problems", [])}
        waiting = {r for s, r in reasons if s == "waiting"}
        last = [pr for pr in p.get("problems", []) if pr.get("state") in ("lastTerminated", "terminated")]
        recent_last = [pr for pr in last if pr.get("state") == "terminated" or in_window(pr.get("finishedAt"), since)]
        oom = [pr for pr in recent_last if pr.get("reason") == "OOMKilled"]
        old_oom = [pr for pr in last if pr.get("reason") == "OOMKilled" and pr not in oom]
        flagged = False

        if old_oom and not oom:
            c = old_oom[0]
            out.append(_finding(
                "oomkilled_earlier", "low", f"{p['name']} container {c['container']} was OOMKilled before this window",
                f"Last OOMKilled at {c.get('finishedAt')}, outside the investigated window; pod has {p['restarts']} restarts in total.",
                [name], next_checks=["Check memory headroom (prom_pod_resources) if the problem recurs"],
            ))
        if oom:
            flagged = True
            c = oom[0]
            out.append(_finding(
                "oomkilled", "high", f"{p['name']} container {c['container']} was OOMKilled",
                f"Container {c['container']} last terminated with reason OOMKilled (exit {c.get('exitCode')}) at {c.get('finishedAt')}; pod has {p['restarts']} restarts.",
                [name], cause="The container exceeded its memory limit (or the node ran out of memory).",
                next_checks=["Compare memory working set with the container memory limit (prom_pod_resources)",
                             "Check whether a recent rollout changed memory limits or workload (k8s_rollout_history)"],
                remediation="Raise the memory limit or fix the memory growth, via a reviewed configuration change.",
            ))
        if "CrashLoopBackOff" in waiting and not oom:
            flagged = True
            term = last[0] if last else {}
            out.append(_finding(
                "crashloop", "high", f"{p['name']} is in CrashLoopBackOff ({p['restarts']} restarts)",
                f"Container keeps exiting (last reason {term.get('reason') or 'unknown'}, exit code {term.get('exitCode')}).",
                [name], cause="The process exits at start-up; common causes are invalid configuration, missing dependencies or a failing entrypoint.",
                next_checks=["Read the previous container's logs (k8s_pod_logs previous=true)", "Describe the pod for env/config references (k8s_describe_pod)"],
                remediation="Fix the configuration or image, or roll back to the last working revision after review.",
            ))
        pulls = waiting & IMAGE_PULL
        if pulls:
            msg = next((pr.get("message") for pr in p["problems"] if pr.get("reason") in pulls), "")
            out.append(_finding(
                "image_pull", "high", f"{p['name']} cannot pull its image ({', '.join(sorted(pulls))})", msg or "Image pull is failing.",
                [name], cause="Wrong image name/tag, missing registry credentials, or registry unreachable.",
                next_checks=["Check the image reference (k8s_describe_pod)", "Check pull-secret events (k8s_events)"],
                remediation="Correct the image reference or the imagePullSecret.",
            ))
        cfg = waiting & CONFIG_ERRORS
        if cfg:
            msg = next((pr.get("message") for pr in p["problems"] if pr.get("reason") in cfg), "")
            out.append(_finding(
                "config_error", "high", f"{p['name']} cannot start: {', '.join(sorted(cfg))}", msg or "Container configuration error.",
                [name], cause="A referenced ConfigMap/Secret or key is missing, or the container spec is invalid.",
                next_checks=["Describe the pod to see env/envFrom references (k8s_describe_pod)"],
            ))
        unsched = [pr for pr in p.get("problems", []) if pr.get("state") == "unschedulable"]
        if unsched:
            out.append(_finding(
                "unschedulable", "high", f"{p['name']} is Pending: cannot be scheduled",
                unsched[0].get("message") or "Scheduler could not place the pod.", [name],
                cause="Insufficient resources, taints, node selectors/affinity, or unbound PVCs.",
                next_checks=["Inspect node capacity, pressure and taints (k8s_nodes)", "Check FailedScheduling events (k8s_events)"],
            ))
        elif p.get("phase") == "Pending" and not waiting and not unsched:
            out.append(_finding("pending", "medium", f"{p['name']} is Pending", "Pod has not started yet.", [name],
                                next_checks=["Check events for the pod (k8s_events)"]))
        ready, total = (p.get("ready") or "0/0").split("/")
        if p.get("phase") == "Running" and ready != total and not waiting and not flagged:
            out.append(_finding(
                "not_ready", "medium", f"{p['name']} is Running but not Ready ({p['ready']})",
                "At least one container is failing its readiness probe.", [name],
                cause="Readiness probe failing: application unhealthy, slow start or wrong probe settings.",
                next_checks=["Check Unhealthy events (k8s_events)", "Describe probes (k8s_describe_pod)"],
            ))
        if p.get("reason") == "Evicted":
            out.append(_finding("evicted", "medium", f"{p['name']} was evicted", "Node pressure evicted the pod.", [name],
                                next_checks=["Check node pressure (k8s_nodes)"]))
        restarts_here = [pr for pr in last if pr.get("state") == "lastTerminated" and in_window(pr.get("finishedAt"), since)]
        if restarts_here and not flagged:
            restarted.append((p, restarts_here[0]))
    if restarted:
        listing = "; ".join(f"{p['name']} ({r.get('reason') or 'unknown'}, exit {r.get('exitCode')}, at {r.get('finishedAt')})" for p, r in restarted[:15])
        out.append(_finding(
            "recent_restarts", "medium", f"{len(restarted)} pod(s) restarted inside the window",
            f"Last container restarts: {listing}.", [f"pod/{p['name']}" for p, _ in restarted[:15]],
            next_checks=["Read the previous container logs of a restarted pod (k8s_pod_logs previous=true)"],
        ))
    return out


def detect_services(services: list[dict]) -> list[dict]:
    out = []
    for s in services:
        if not s.get("selector"):
            continue  # headless/external services without selectors are managed manually
        name = f"service/{s['name']}"
        eps = s.get("endpoints") or {}
        if s.get("matchingPodCount", 0) == 0:
            near = s.get("nearMissPods") or []
            hint = ""
            if near:
                hint = " Pods with similar labels exist: " + "; ".join(f"{n['pod']} {n['labels']}" for n in near[:3]) + "."
            out.append(_finding(
                "service_selector_mismatch", "high", f"Service {s['name']} selector matches no pods",
                f"Selector {s['selector']} matches 0 pods, so the service has no endpoints and proxies return 502/503.{hint}",
                [name], cause="The Service selector does not match the labels of the intended pods (typo or label change).",
                next_checks=["Compare the selector with the Deployment's pod labels (k8s_workloads)"],
                remediation="Patch the Service selector (or pod labels) through a reviewed configuration change.",
            ))
        elif eps.get("ready", 0) == 0:
            out.append(_finding(
                "service_no_ready_endpoints", "high", f"Service {s['name']} has no ready endpoints",
                f"{s['matchingPodCount']} pods match the selector but none are ready ({eps.get('notReady', 0)} not-ready addresses).",
                [name], cause="The selected pods are failing readiness or crashing.",
                next_checks=["Check the matching pods' status and readiness probes (k8s_list_pods)"],
            ))
        named_ports = {n for n, _ in s.get("podContainerPorts") or [] if n}
        numbers = {num for _, num in s.get("podContainerPorts") or []}
        for port in s.get("ports") or []:
            tp = port.get("targetPort")
            if isinstance(tp, str) and s.get("matchingPodCount") and tp not in named_ports:
                out.append(_finding(
                    "service_target_port", "high", f"Service {s['name']} targets port name {tp!r} that its pods do not declare",
                    f"Pods declare named ports {sorted(named_ports) or 'none'}.", [name],
                    cause="Named targetPort does not exist on the selected pods.",
                ))
            elif isinstance(tp, int) and numbers and s.get("matchingPodCount") and tp not in numbers:
                out.append(_finding(
                    "service_target_port", "low", f"Service {s['name']} targetPort {tp} is not a declared container port",
                    f"Declared container ports: {sorted(n for n in numbers if n)}. Undeclared ports can still work if the process listens there.",
                    [name],
                ))
    return out


def detect_workloads(workloads: list[dict]) -> list[dict]:
    out = []
    for w in workloads:
        if w.get("desired", 0) and (w.get("ready") or 0) < w["desired"]:
            stalled = next((c for c in w.get("conditions", []) if c["type"] == "Progressing" and c.get("reason") == "ProgressDeadlineExceeded"), None)
            out.append(_finding(
                "workload_unavailable", "high" if not w.get("ready") else "medium",
                f"{w['kind']} {w['name']} has {w.get('ready') or 0}/{w['desired']} ready replicas",
                ("Rollout exceeded its progress deadline. " if stalled else "") + f"Images: {', '.join(w.get('images') or [])}.",
                [f"{w['kind'].lower()}/{w['name']}"],
                next_checks=["Check the rollout history for a recent change (k8s_rollout_history)"],
            ))
    return out


def detect_events(events: list[dict]) -> list[dict]:
    warnings = [e for e in events if e.get("type") == "Warning"]
    if not warnings:
        return []
    counts = Counter(e.get("reason") for e in warnings)
    interesting = {"FailedScheduling", "FailedMount", "FailedAttachVolume", "BackOff", "Unhealthy", "FailedCreatePodSandBox",
                   "DNSConfigForming", "Evicted", "OOMKilling", "FailedCreate", "NodeNotReady", "ProbeWarning"}
    out = []
    for reason, n in counts.most_common(6):
        if reason not in interesting:
            continue
        sample = next(e for e in warnings if e.get("reason") == reason)
        out.append(_finding(
            f"event_{reason}", "medium", f"{n}× {reason} warning events",
            f"Latest: {sample['object']}: {sample['message']}", [sample["object"].lower()],
        ))
    return out


def detect_ingresses(ingresses: list[dict], services: list[dict]) -> list[dict]:
    names = {s["name"] for s in services}
    out = []
    for ing in ingresses:
        for r in ing.get("rules", []):
            if r.get("service") and r["service"] not in names:
                out.append(_finding(
                    "ingress_backend_missing", "high", f"Ingress {ing['name']} routes {r.get('host') or '*'}{r.get('path') or ''} to missing service {r['service']}",
                    "The backend Service does not exist in this namespace; the ingress controller returns 404/503.",
                    [f"ingress/{ing['name']}"],
                ))
    return out


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def triage(pods: list[dict], events: list[dict], services: list[dict], workloads: list[dict], ingresses: list[dict],
           since: datetime | None = None) -> list[dict]:
    events = [e for e in events if in_window(e.get("lastSeen"), since)]
    findings = detect_pods(pods, since) + detect_services(services) + detect_workloads(workloads) + detect_events(events) + \
        detect_ingresses(ingresses, services)
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 3))
    return findings
