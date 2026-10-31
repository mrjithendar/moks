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
    return {"name": meta.get("name"), "project": spec.get("project"), "namespace": (spec.get("destination") or {}).get("namespace") or "", "sources": sources,
            "sync": (st.get("sync") or {}).get("status") or "Unknown", "health": (st.get("health") or {}).get("status") or "Unknown",
            "auto_sync": bool((spec.get("syncPolicy") or {}).get("automated")), "tracked_resources": len(st.get("resources") or []),
            "operation": {"phase": op.get("phase"), "finished": op.get("finishedAt"), "message": reason(op.get("message")), "failed_resources": failed[:MAX_FAILED_RESOURCES]} if op else None,
            "conditions": conditions, "errors": errors, "missing": _missing(errors, sources)}


def is_problem(row: dict) -> bool:
    op = row["operation"] or {}
    return bool(row["errors"]) or op.get("phase") in FAILED_PHASES or row["health"] in BAD_HEALTH or row["sync"] != "Synced"


def finding(row: dict) -> dict[str, Any] | None:
    """Deterministic: each states what Argo CD reported, and the LLM may only explain it."""
    name, op, missing = row["name"], row["operation"] or {}, row["missing"]
    down = row["health"] in BAD_HEALTH
    base = {"severity": "high" if down else "medium", "resources": [f"application/{name}"], "namespace": row["namespace"]}
    running = "Its running workloads are not affected, but Argo CD cannot compare or update them." if not down else f"Argo CD reports its health as {row['health']}."
    never_prune = "Sync without prune: a sync with prune from a wrong source can delete what the app tracks."
    if missing:
        found = "; ".join(f"{c['repository']}:{c['path']}" for c in missing.get("candidates") or [])
        where = f" Git has a similar path at {found}." if found else ""
        return {**base, "kind": "argocd_source_missing", "title": f"Argo CD app {name} points at a path that does not exist",
                "detail": f"It reads {missing['path']} from {missing['repo'] or 'its source repository'}, which Argo CD cannot find. {running}{where}",
                "suspected_cause": "The files were moved or removed in Git, and the live Application spec was not updated to match.",
                "next_checks": [f"Compare the live spec of {name} with the Application file in Git", "Check whether an app that replaced it exists in Argo CD"],
                "remediation_hint": f"Correct the source of {name} in its Application manifest, apply it, then check the diff. {never_prune}"}
    if row["errors"]:
        first = row["errors"][0]
        return {**base, "kind": "argocd_comparison_error", "title": f"Argo CD app {name} reports {first['type']}", "detail": f"{first['message']} {running}",
                "suspected_cause": "Argo CD could not build or compare this app's desired state.",
                "next_checks": [f"Read the conditions of Application {name}"], "remediation_hint": f"Fix what the condition names, then sync {name}. {never_prune}"}
    if op.get("phase") in FAILED_PHASES:
        failed = op.get("failed_resources") or []
        which = "; ".join(f"{r['kind']}/{r['name']}: {r['message']}" for r in failed)
        return {**base, "kind": "argocd_sync_failed", "title": f"The last Argo CD sync of {name} ended {op['phase']}", "detail": which or op.get("message") or "Argo CD gave no message.",
                "suspected_cause": "A resource in this sync was rejected or failed.", "next_checks": [f"Read the last operation of Application {name}"],
                "remediation_hint": f"Fix what the sync reported, then sync {name} again. {never_prune}"}
    if down:
        return {**base, "kind": "argocd_unhealthy", "title": f"Argo CD reports {name} as {row['health']}", "detail": f"Sync status is {row['sync']}.",
                "next_checks": [f"Inspect the workloads in namespace {row['namespace']}"]}
    if row["sync"] != "Synced":
        return {**base, "severity": "low", "kind": "argocd_out_of_sync", "title": f"Argo CD app {name} is {row['sync']}", "detail": "The cluster differs from Git, or Argo CD has not compared them.",
                "next_checks": [f"Review the diff of {name} before syncing"]}
    return None


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
    return state
