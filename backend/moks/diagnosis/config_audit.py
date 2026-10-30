"""Configuration hygiene from pod specs and events only.

MOKS never reads Secret or ConfigMap contents (its ClusterRole excludes them). Everything here
comes from what pods declare (env, envFrom, volumes, resources, probes, security context, image)
and what Kubernetes reports (events such as `secret "x" not found`). Literal env values are
inspected only by name and never returned.
"""
from __future__ import annotations

import re
from typing import Any

SENSITIVE_NAME = re.compile(r"(?i)(pass|secret|token|api[_-]?key|private|credential|dsn|auth|cert|signature|cookie|session)")
MISSING_REF = re.compile(r'(?i)(configmap|secret)s? "([a-z0-9.-]+)" not found|couldn.t find key (\S+) in (ConfigMap|Secret) (\S+)')


def _owner(pod: dict) -> str:
    meta = pod["metadata"]
    o = (meta.get("ownerReferences") or [{}])[0]
    name = o.get("name") or meta["name"]
    h = (meta.get("labels") or {}).get("pod-template-hash")
    if o.get("kind") == "ReplicaSet" and h and name.endswith("-" + h):
        return f"Deployment/{name[: -len(h) - 1]}"
    return f"{o.get('kind', 'Pod')}/{name}"


def audit(pods: list[dict], events: list[dict]) -> dict[str, Any]:
    by_workload: dict[str, dict] = {}
    for p in pods:
        wl = _owner(p)
        if wl in by_workload:
            continue  # one representative pod per workload
        spec = p.get("spec", {})
        pod_sec = spec.get("securityContext") or {}
        issues: list[dict] = []
        refs = {"secrets": set(), "configmaps": set()}
        for v in spec.get("volumes") or []:
            if v.get("secret"):
                refs["secrets"].add(v["secret"].get("secretName"))
            if v.get("configMap"):
                refs["configmaps"].add(v["configMap"].get("name"))
            if v.get("hostPath"):
                issues.append({"kind": "host_path_volume", "severity": "high", "detail": f"hostPath volume {v['name']} ({v['hostPath'].get('path')})"})
        for c in spec.get("containers", []):
            cname = c["name"]
            res = c.get("resources") or {}
            req, lim = res.get("requests") or {}, res.get("limits") or {}
            if not req.get("cpu") or not req.get("memory"):
                issues.append({"kind": "missing_requests", "severity": "medium", "container": cname,
                               "detail": f"{cname} has no {'/'.join(k for k in ('cpu', 'memory') if not req.get(k))} request: the scheduler cannot reserve capacity"})
            if not lim.get("memory"):
                issues.append({"kind": "missing_memory_limit", "severity": "medium", "container": cname,
                               "detail": f"{cname} has no memory limit: a leak can starve the node"})
            if not c.get("readinessProbe"):
                issues.append({"kind": "missing_readiness_probe", "severity": "medium", "container": cname,
                               "detail": f"{cname} has no readiness probe: traffic reaches it before it is ready"})
            if not c.get("livenessProbe"):
                issues.append({"kind": "missing_liveness_probe", "severity": "low", "container": cname,
                               "detail": f"{cname} has no liveness probe: a hung process is never restarted"})
            image = c.get("image", "")
            tag = image.split("@")[0].rsplit(":", 1)[-1] if ":" in image.split("/")[-1] else ""
            if "@sha256:" not in image and tag in ("", "latest"):
                issues.append({"kind": "mutable_image_tag", "severity": "medium", "container": cname,
                               "detail": f"{cname} runs {image or '?'} ({'no tag' if not tag else ':latest'}): rollouts are not reproducible; pin a version or digest"})
            sec = c.get("securityContext") or {}
            if sec.get("privileged"):
                issues.append({"kind": "privileged_container", "severity": "high", "container": cname, "detail": f"{cname} runs privileged"})
            if sec.get("allowPrivilegeEscalation", True) and not sec.get("privileged"):
                issues.append({"kind": "privilege_escalation_allowed", "severity": "low", "container": cname,
                               "detail": f"{cname} does not set allowPrivilegeEscalation: false"})
            if not (sec.get("runAsNonRoot") or pod_sec.get("runAsNonRoot")) and (sec.get("runAsUser", pod_sec.get("runAsUser")) in (None, 0)):
                issues.append({"kind": "may_run_as_root", "severity": "low", "container": cname, "detail": f"{cname} may run as root (runAsNonRoot not set)"})
            for e in c.get("env") or []:
                if "value" in e and SENSITIVE_NAME.search(e["name"]) and e.get("value"):
                    issues.append({"kind": "plaintext_secret_env", "severity": "high", "container": cname,
                                   "detail": f"{cname} sets {e['name']} as a literal env value (value not shown): move it to a Secret and reference it with secretKeyRef"})
                vf = e.get("valueFrom") or {}
                if vf.get("secretKeyRef"):
                    refs["secrets"].add(vf["secretKeyRef"].get("name"))
                    if vf["secretKeyRef"].get("optional"):
                        issues.append({"kind": "optional_secret_ref", "severity": "low", "container": cname,
                                       "detail": f"{cname} env {e['name']} uses an optional Secret key: a missing secret starts the app without it"})
                if vf.get("configMapKeyRef"):
                    refs["configmaps"].add(vf["configMapKeyRef"].get("name"))
            for ef in c.get("envFrom") or []:
                if ef.get("secretRef"):
                    refs["secrets"].add(ef["secretRef"].get("name"))
                    issues.append({"kind": "secret_envfrom", "severity": "low", "container": cname,
                                   "detail": f"{cname} imports every key of Secret {ef['secretRef'].get('name')} as env vars: prefer specific keys or a volume (env values leak into crash dumps and child processes)"})
                if ef.get("configMapRef"):
                    refs["configmaps"].add(ef["configMapRef"].get("name"))
        if spec.get("automountServiceAccountToken") is not False and spec.get("serviceAccountName", "default") == "default":
            issues.append({"kind": "default_sa_token", "severity": "low", "detail": "uses the default ServiceAccount with its token mounted; set automountServiceAccountToken: false if the app does not call the API"})
        by_workload[wl] = {"workload": wl, "namespace": p["metadata"].get("namespace"), "issues": issues,
                           "references": {k: sorted(x for x in v if x) for k, v in refs.items()}}
    missing = []
    for e in events:
        m = MISSING_REF.search(e.get("message", ""))
        if m:
            kind = (m.group(1) or m.group(4) or "").lower()
            name = m.group(2) or m.group(5)
            missing.append({"object": e.get("object"), "kind": kind, "name": (name or "").strip('"'), "key": m.group(3),
                            "message": e.get("message", "")[:300], "lastSeen": e.get("lastSeen")})
    counts: dict[str, int] = {}
    for w in by_workload.values():
        for i in w["issues"]:
            counts[i["kind"]] = counts.get(i["kind"], 0) + 1
    return {"workloads": sorted(by_workload.values(), key=lambda w: -sum(i["severity"] == "high" for i in w["issues"])),
            "missing_references": missing, "issue_counts": counts}


SEV = {"high": 0, "medium": 1, "low": 2}


def findings_from(audit_data: dict, namespace: str, ref: str) -> list[dict]:
    """Detector-style findings (high/medium only, grouped by kind) so skills can react to them."""
    out = []
    for m in audit_data.get("missing_references", [])[:10]:
        out.append({"kind": "config_ref_missing", "severity": "high", "title": f"{m['object']} references a missing {m['kind']} {m['name']}" + (f" key {m['key']}" if m.get("key") else ""),
                    "detail": m["message"], "resources": [m["object"].lower()], "namespace": namespace, "evidence": [ref],
                    "suspected_cause": "The referenced ConfigMap/Secret (or key) was not created, was renamed, or lives in another namespace.",
                    "next_checks": ["Compare the reference with the manifests in Git (git_read_file)"], "remediation_hint": "Restore the object/key through Git, not by hand."})
    grouped: dict[str, list] = {}
    for w in audit_data.get("workloads", []):
        for i in w["issues"]:
            if SEV[i["severity"]] <= 1:
                grouped.setdefault(i["kind"], []).append((w["workload"], i))
    for kind, items in sorted(grouped.items(), key=lambda kv: SEV[kv[1][0][1]["severity"]]):
        out.append({"kind": kind, "severity": items[0][1]["severity"],
                    "title": f"{len(items)} workload(s): {kind.replace('_', ' ')}",
                    "detail": "; ".join(f"{wl}: {i['detail']}" for wl, i in items[:6]),
                    "resources": [wl.lower() for wl, _ in items[:10]], "namespace": namespace, "evidence": [ref],
                    "suspected_cause": None, "next_checks": [], "remediation_hint": None})
    return out
