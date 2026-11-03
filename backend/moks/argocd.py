"""Argo CD, read-only: what each Application reports about itself.

Workloads keep running when Argo CD cannot sync, so the cluster alone looks healthy. The reason only
exists on the Application object, which MOKS reads through the Kubernetes API (ClusterRole moks-reader
with argocd.enabled). It never syncs, refreshes or patches.

A row holds source locations and status only. Helm parameters and inline values can hold secrets, so
they are never copied out of the spec, and credentials in a repository URL are removed.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .config import get_settings
from .connectors.base import ConnectorError
from .redaction import redact

GROUP = "argoproj.io"
VERSION = "v1alpha1"
PLURAL = "applications"
FAILED_PHASES = {"Failed", "Error"}
BAD_HEALTH = {"Degraded", "Missing"}
MAX_MESSAGE = 500
MAX_FAILED_RESOURCES = 5
MAX_LOOKUPS = 12
MAX_REPOSITORIES = 6
MAX_CANDIDATES = 3

_PREFIX = re.compile(r"^.*?rpc error: code = \w+ desc = ", re.S)
_HELM_EXIT = re.compile(r"failed exit status \d+: (.*)", re.S)
_PATH_GONE = re.compile(r"^(?P<path>[^\s:]+): app path does not exist")
_FILE_GONE = re.compile(r"open <path to cached source>/(?P<path>[^\s:]+): no such file or directory")
_GITHUB = re.compile(r"github\.com[/:]([\w.-]+/[\w.-]+?)(?:\.git)?/?$")
_UNREACHABLE = re.compile(r"(?i)dial tcp|no such host|i/o timeout|connection refused|context deadline exceeded|tls handshake timeout|unable to connect|"
                          r"failed to get cluster info|cluster .{0,80} not found|the server could not find the requested resource \(get namespaces\)|network is unreachable")
_LOOKUP_HOST = re.compile(r"(?i)lookup ([a-z0-9][a-z0-9.-]+\.[a-z]{2,})|https?://([a-z0-9][a-z0-9.-]+)(?::\d+)?|dial tcp ([0-9.]+|[a-z0-9.-]+\.[a-z]{2,}):\d+")
_REPO_ACCESS = re.compile(r"(?i)authentication required|repository not found|could not read username|permission denied \(publickey\)|invalid username or password|unable to resolve git revision")
_BUILD = re.compile(r"(?i)helm template|kustomize|failed to unmarshal|yaml: |app path does not exist|no such file or directory|error converting yaml|failed exit status")


def namespace() -> str:
    return get_settings().argocd_namespace


def reason(message: str | None) -> str:
    """The cause, without the transport wrapping Argo CD puts around it."""
    text = _PREFIX.sub("", (message or "").strip()).removeprefix("Manifest generation error (cached): ")
    helm = _HELM_EXIT.search(text)
    return redact((helm.group(1) if helm else text).strip())[:MAX_MESSAGE]


def _source(s: dict) -> dict[str, Any]:
    url = re.sub(r"//[^/@]+@", "//", s.get("repoURL") or "")
    return {k: v for k, v in {"repo": url, "path": s.get("path"), "chart": s.get("chart"), "revision": s.get("targetRevision"), "ref": s.get("ref")}.items() if v}


def _missing(errors: list[dict], sources: list[dict]) -> dict[str, Any] | None:
    """The path a failed manifest build could not find, and the repository Argo CD looked in."""
    for e in errors:
        gone = _PATH_GONE.search(e["message"])
        if gone:
            repo = next((s["repo"] for s in sources if s.get("path") == gone["path"]), None)
            return {"path": gone["path"], "repo": repo}
        gone = _FILE_GONE.search(e["message"])
        if gone:
            repo = next((s["repo"] for s in sources if s.get("ref")), None)
            return {"path": gone["path"], "repo": repo}
    return None


def app_row(app: dict) -> dict[str, Any]:
    spec, st, meta = app.get("spec") or {}, app.get("status") or {}, app.get("metadata") or {}
    sources = [_source(s) for s in (spec.get("sources") or ([spec["source"]] if spec.get("source") else []))]
    conditions = [{"type": c.get("type"), "message": reason(c.get("message"))} for c in st.get("conditions") or []]
    errors = [c for c in conditions if not (c["type"] or "").endswith("Warning")]
    op = st.get("operationState") or {}
    failed = [{"kind": r.get("kind"), "name": r.get("name"), "message": redact(r.get("message") or "")[:MAX_MESSAGE]}
              for r in (op.get("syncResult") or {}).get("resources") or [] if r.get("status") not in ("Synced", "Pruned", None)]
    dest = spec.get("destination") or {}
    return {"name": meta.get("name"), "project": spec.get("project"), "namespace": dest.get("namespace") or "", "sources": sources,
            "destination": re.sub(r"//[^/@]+@", "//", dest.get("server") or dest.get("name") or ""),
            "sync": (st.get("sync") or {}).get("status") or "Unknown", "health": (st.get("health") or {}).get("status") or "Unknown",
            "auto_sync": bool((spec.get("syncPolicy") or {}).get("automated")), "tracked_resources": len(st.get("resources") or []),
            "operation": {"phase": op.get("phase"), "finished": op.get("finishedAt"), "message": reason(op.get("message")), "failed_resources": failed[:MAX_FAILED_RESOURCES]} if op else None,
            "conditions": conditions, "errors": errors, "missing": _missing(errors, sources)}


def is_problem(row: dict) -> bool:
    op = row["operation"] or {}
    return bool(row["errors"]) or op.get("phase") in FAILED_PHASES or row["health"] in BAD_HEALTH or row["sync"] != "Synced"


def classify_error(row: dict, message: str) -> dict[str, Any]:
    """What kind of failure a condition message describes, and the key that failures sharing one cause share.
    Only a concrete key (a host, a repository) can group apps: messages that merely look alike stay apart."""
    if _UNREACHABLE.search(message):
        m = _LOOKUP_HOST.search(message)
        host = next((g for g in (m.groups() if m else ()) if g), None) or re.sub(r"^https?://", "", row.get("destination") or "").split("/")[0] or None
        return {"category": "destination_unreachable", "key": host}
    if _REPO_ACCESS.search(message):
        return {"category": "repository_access", "key": next((s["repo"] for s in row["sources"] if s.get("repo")), None)}
    if _BUILD.search(message):
        return {"category": "manifest_generation", "key": None}
    return {"category": "other", "key": None}


def _next_step(name: str, row: dict, cause: dict, message: str) -> tuple[list[str], str]:
    """A concrete check for this kind of failure, never "fix what it says"."""
    ns = namespace()
    read = f"kubectl -n {ns} get application {name} -o jsonpath='{{.status.conditions}}'"
    if cause["category"] == "destination_unreachable":
        host = cause["key"] or "its destination cluster"
        return ([f"Check whether {host}, the cluster {name} deploys to, still exists and resolves (Argo CD reports: {message[:160]})",
                 f"List the clusters Argo CD knows (`argocd cluster list`, or the Secrets labelled argocd.argoproj.io/secret-type=cluster in {ns}) and compare with {row.get('destination') or 'the Application destination'}"],
                f"If {host} was removed on purpose, delete or retarget Application {name} through its manifest in Git; if not, restore the cluster or its registration in Argo CD.")
    if cause["category"] == "repository_access":
        return ([f"Check the repository credentials Argo CD uses for {cause['key'] or 'this source'} (`argocd repo list`)", f"Read the conditions: `{read}`"],
                f"Fix the repository URL or credentials for {name}'s source, then refresh it.")
    if cause["category"] == "manifest_generation":
        src = next((s for s in row["sources"] if s.get("path") or s.get("chart")), {})
        where = f"{src.get('path') or src.get('chart')} at {src.get('revision') or 'its target revision'}" if src else "its source"
        return ([f"Rebuild the manifests of {name} from {where} locally (kustomize build / helm template) to reproduce: {message[:160]}", f"Read the conditions: `{read}`"],
                f"Correct {where} so its manifests build, then sync {name}.")
    return ([f"Read the conditions of Application {name}: `{read}`", f"Read the argocd-application-controller logs for {name} around the last reconcile"],
            f"Address the condition Argo CD reports for {name} ({message[:120]}), then sync {name}.")


def finding(row: dict) -> dict[str, Any] | None:
    """Deterministic: each states what Argo CD reported, and the LLM may only explain it. Nothing here
    says anything about the app's running workloads: that needs workload evidence MOKS reads separately."""
    name, op, missing = row["name"], row["operation"] or {}, row["missing"]
    down = row["health"] in BAD_HEALTH
    base = {"severity": "high" if down else "medium", "resources": [f"application/{name}"], "namespace": row["namespace"], "destination": row.get("destination") or None}
    running = ("Argo CD cannot compare or sync this app until that is fixed. Its running workloads were not checked here, so their state is unknown."
               if not down else f"Argo CD reports its health as {row['health']}.")
    never_prune = "Sync without prune: a sync with prune from a wrong source can delete what the app tracks."
    if missing:
        found = "; ".join(f"{c['repository']}:{c['path']}" for c in missing.get("candidates") or [])
        where = f" Git has a similar path at {found}." if found else ""
        return {**base, "kind": "argocd_source_missing", "title": f"Argo CD app {name} points at a path that does not exist",
                "detail": f"It reads {missing['path']} from {missing['repo'] or 'its source repository'}, which Argo CD cannot find. {running}{where}",
                "cause": {"category": "manifest_generation", "key": None},
                "suspected_cause": "The files were moved or removed in Git, and the live Application spec was not updated to match.",
                "next_checks": [f"Compare the live spec of {name} with the Application file in Git", "Check whether an app that replaced it exists in Argo CD"],
                "remediation_hint": f"Correct the source of {name} in its Application manifest, apply it, then check the diff. {never_prune}"}
    if row["errors"]:
        first = row["errors"][0]
        cause = classify_error(row, first["message"])
        checks, hint = _next_step(name, row, cause, first["message"])
        why = {"destination_unreachable": "Argo CD could not reach the cluster this app deploys to.", "repository_access": "Argo CD could not read this app's source repository.",
               "manifest_generation": "Argo CD could not build this app's manifests."}.get(cause["category"], "Argo CD could not build or compare this app's desired state.")
        return {**base, "kind": "argocd_comparison_error", "title": f"Argo CD app {name} reports {first['type']}", "detail": f"{first['message']} {running}",
                "cause": cause, "suspected_cause": why, "next_checks": checks, "remediation_hint": f"{hint} {never_prune}"}
    if op.get("phase") in FAILED_PHASES:
        failed = op.get("failed_resources") or []
        which = "; ".join(f"{r['kind']}/{r['name']}: {r['message']}" for r in failed)
        first = failed[0] if failed else None
        return {**base, "kind": "argocd_sync_failed", "title": f"The last Argo CD sync of {name} ended {op['phase']}", "detail": which or op.get("message") or "Argo CD gave no message.",
                "cause": {"category": "sync", "key": None}, "suspected_cause": "A resource in this sync was rejected or failed.",
                "next_checks": [f"Read the last operation of Application {name}: `kubectl -n {namespace()} get application {name} -o jsonpath='{{.status.operationState}}'`"],
                "remediation_hint": (f"Correct {first['kind']}/{first['name']} in {name}'s source ({first['message'][:120]}), then sync {name} again. {never_prune}" if first
                                     else f"Read why the last operation of {name} ended {op['phase']} before syncing it again. {never_prune}")}
    if down:
        return {**base, "kind": "argocd_unhealthy", "title": f"Argo CD reports {name} as {row['health']}", "detail": f"Sync status is {row['sync']}.",
                "next_checks": [f"Inspect the workloads in namespace {row['namespace']}"]}
    if row["sync"] != "Synced":
        return {**base, "severity": "low", "kind": "argocd_out_of_sync", "title": f"Argo CD app {name} is {row['sync']}", "detail": "The cluster differs from Git, or Argo CD has not compared them.",
                "next_checks": [f"Review the diff of {name} before syncing"]}
    return None


GROUPABLE = {"destination_unreachable", "repository_access"}


def group(findings: list[dict]) -> list[dict]:
    """Apps that fail for one supported shared cause become one finding that keeps every affected app and its
    evidence. The cause must have a concrete key (the same unreachable host, the same repository): similar
    wording is not enough, and every other finding is returned as it was."""
    buckets: dict[tuple, list[dict]] = {}
    rest: list[dict] = []
    for f in findings:
        c = f.get("cause") or {}
        if f.get("kind") == "argocd_comparison_error" and c.get("category") in GROUPABLE and c.get("key"):
            buckets.setdefault((c["category"], c["key"]), []).append(f)
        else:
            rest.append(f)
    out: list[dict] = []
    for (category, key), rows in buckets.items():
        if len(rows) < 2:
            rest.extend(rows)
            continue
        apps = sorted(r["resources"][0].partition("/")[2] for r in rows)
        what = f"cannot reach {key}" if category == "destination_unreachable" else f"cannot read repository {key}"
        sample = rows[0]
        out.append({"kind": "argocd_shared_cause", "severity": max((r["severity"] for r in rows), key=lambda s: {"high": 2, "medium": 1, "low": 0}[s]),
                    "title": f"{len(rows)} Argo CD applications {what}",
                    "detail": f"Each reports the same {category.replace('_', ' ')} for {key}: {', '.join(apps[:12])}{' and more' if len(apps) > 12 else ''}. "
                              "One cause explains them all, so fixing it is one change. Their running workloads were not checked here.",
                    "resources": [r["resources"][0] for r in rows], "namespace": None, "namespaces": sorted({r["namespace"] for r in rows if r.get("namespace")}),
                    "cause": {"category": category, "key": key}, "affected": [{"application": r["resources"][0], "namespace": r.get("namespace"), "detail": r["detail"][:300], "evidence": r.get("evidence", [])} for r in rows],
                    "suspected_cause": sample.get("suspected_cause"), "next_checks": sample.get("next_checks", []), "remediation_hint": sample.get("remediation_hint"),
                    "evidence": sorted({ref for r in rows for ref in r.get("evidence", [])}, key=lambda x: int(x[1:]) if x[1:].isdigit() else 0)})
    return out + rest


def _found_in(paths: list[str], target: str) -> list[str]:
    """Where target's last folder, or its last two segments for a file, sits in this repository."""
    parts = target.split("/")
    tail = "/".join(parts[-2:]) if "." in parts[-1] else parts[-1]
    hits = set()
    for p in paths:
        full = f"/{p}/"
        at = full.find(f"/{tail}/")
        if at >= 0:
            hits.add(full[1:at + 1 + len(tail)])
    return sorted(hits, key=lambda h: (h != target, len(h), h))


async def locate(git: Any, rows: list[dict]) -> list[str]:
    """Candidates only, from allowlisted repositories: a person decides which one is right. Returns notes."""
    wanted = [r["missing"] for r in rows if r["missing"]][:MAX_LOOKUPS]
    notes: list[str] = []
    if not wanted:
        return notes
    trees: dict[str, list[str]] = {}
    for repo in git.repositories[:MAX_REPOSITORIES]:
        try:
            trees[repo] = await git.list_paths(repo)
        except ConnectorError as exc:
            notes.append(f"{repo} was not searched: {exc.message}")
    for m in wanted:
        source = _GITHUB.search(m["repo"] or "")
        hits = [(h != m["path"], repo != (source.group(1) if source else None), {"repository": repo, "path": h}) for repo, paths in trees.items() for h in _found_in(paths, m["path"])]
        m["candidates"] = [h for _, _, h in sorted(hits, key=lambda x: (x[0], x[1], len(x[2]["path"]), x[2]["repository"], x[2]["path"]))][:MAX_CANDIDATES]
    return notes


async def status(k8s: Any, allowed: Callable[[str], bool], git: Any = None, application: str | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {"installed": True, "total": 0, "healthy": [], "problems": [], "findings": [], "notes": []}
    try:
        items = await k8s.argocd_list()
    except ConnectorError as exc:
        if exc.kind not in ("not_found", "forbidden"):
            raise
        state["installed"] = False
        state["error"] = "Argo CD is not installed in this cluster." if exc.kind == "not_found" else "MOKS has no read access to Argo CD here (set argocd.enabled in the chart)."
        return state
    rows = [r for r in (app_row(a) for a in items) if allowed(r["namespace"]) and (application is None or r["name"] == application)]
    state["total"] = len(rows)
    state["healthy"] = sorted(r["name"] for r in rows if not is_problem(r))
    state["problems"] = sorted((r for r in rows if is_problem(r)), key=lambda r: r["name"])
    if git is not None:
        state["notes"] = await locate(git, state["problems"])
    state["findings"] = [f for f in map(finding, state["problems"]) if f]
    state["shared_causes"] = [{"cause": g["cause"], "applications": [r.partition("/")[2] for r in g["resources"]]} for g in group(state["findings"]) if g["kind"] == "argocd_shared_cause"]
    return state
