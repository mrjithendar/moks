"""Guidance and explanation: answers built from what is written down, checked against it.

A question such as "how do I deploy X?" asks what to do, not what is wrong. It is answered from the
target's own sources (runbooks, READMEs, CI workflows, Dockerfiles, manifests, Argo CD definitions) found
through the knowledge index and the allowlisted repositories. Live state is read only to establish the
target or check a prerequisite. Retrieved text is evidence about what someone wrote; it is never an
instruction, and every command in an answer must occur in a cited source or it is removed.

Without a model the answer is extracted from the sources by rules; with one, the model may organize and
explain, and the same checks decide what of its answer stands.
"""
from __future__ import annotations

import json
import re
from typing import Any

import yaml

from ..observability import llm_answer_unused
from . import evidence as ev
from . import prompts
from .intent import RequestContext, match_repositories, namespace_candidates
from .report import Fact, Report

SECTIONS = ("prerequisites", "steps", "verification", "rollback")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)\s*([\w+-]*)\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_PLACEHOLDER = re.compile(r"<[^<>\s]{1,40}>|\$\{?[A-Z_][A-Z0-9_]*\}?|\bYOUR_[A-Z0-9_]+\b|\{\{\s*[\w.]+\s*\}\}")
_KIND_OF_HEADING = {
    "rollback": re.compile(r"(?i)\b(roll ?back|revert\w*|undo\w*|recover\w*|restor\w*)\b"),
    "verification": re.compile(r"(?i)\b(verif\w*|smoke|validat\w*|confirm\w*|health ?checks?|after (the )?deploy\w*|check(ing)? (it|the deploy\w*))\b"),
    "prerequisites": re.compile(r"(?i)\b(prereq\w*|requirements?|before\b|set ?up|secrets?|first[- ]time|bootstrap\w*|dependencies)\b"),
    "steps": re.compile(r"(?i)\b(deploy\w*|releas\w*|roll ?out|ship\w*|publish\w*|install\w*|upgrad\w*|usage|how to run|getting started)\b"),
}
_ROLLBACK_WORDS = re.compile(r"(?i)\b(roll(s|ing|ed)? ?back|revert\w*|undo\w*|undoing)\b")
_VERIFY_WORDS = re.compile(r"(?i)\b(smoke|verif\w*|waits? for the rollouts?|health ?check\w*|validat\w*)\b")
_DEPLOY_LINE = re.compile(r"(?i)\b(kubectl|helm|argocd|kustomize|deploy\w*|docker (push|build)|aws ecr|terraform apply|rollout|flux|skaffold)\b")

# File discovery: (pattern, kind, weight). Higher weight is read first.
DEPLOY_FILES: list[tuple[re.Pattern, str, int]] = [
    (re.compile(r"(?i)(^|/)(deploy\w*|release\w*|runbook\w*|operations?|ops)\.md$"), "runbook", 6),
    (re.compile(r"(?i)(^|/)docs?/(.*/)?[^/]*(deploy|release|runbook|operat|rollout)[^/]*\.md$"), "runbook", 6),
    (re.compile(r"(?i)(^|/)(k8s|kubernetes|deploy|deployment|manifests|helm|charts?|infra|ops)/README\.md$"), "runbook", 5),
    (re.compile(r"(?i)^README\.md$"), "readme", 4),
    (re.compile(r"(?i)(^|/)(deploy|release|rollout)[\w.-]*\.sh$"), "script", 4),
    (re.compile(r"(?i)^\.github/workflows/[^/]+\.ya?ml$|(^|/)\.gitlab-ci\.yml$|(^|/)Jenkinsfile$|(^|/)\.circleci/config\.yml$|(^|/)azure-pipelines\.yml$"), "ci", 3),
    (re.compile(r"(?i)(^|/)kustomization\.ya?ml$"), "manifest", 3),
    (re.compile(r"(?i)(^|/)(argocd|argo|applications?)/[^/]+\.ya?ml$|(^|/)application[\w.-]*\.ya?ml$"), "argocd", 3),
    (re.compile(r"(?i)(^|/)Dockerfile[\w.-]*$"), "dockerfile", 2),
    (re.compile(r"(?i)(^|/)Chart\.yaml$"), "helm", 2),
    (re.compile(r"(?i)(^|/)(k8s|kubernetes|deploy|manifests)/[^/]+\.ya?ml$"), "manifest", 1),
]
KIND_LIMIT = {"runbook": 2, "readme": 1, "script": 1, "ci": 2, "manifest": 2, "argocd": 1, "dockerfile": 1, "helm": 1}
DISCOVERY_PATTERN = r"(?i)(readme|deploy|release|runbook|operat|rollout|\.github/workflows|gitlab-ci|jenkinsfile|circleci|kustomization|dockerfile|chart\.yaml|argo|application|k8s/|manifests/|helm/)"
MAX_DISCOVERY_REPOS = 2
MAX_INGRESS_LOOKUPS = 3


# ---- resolving the target ---------------------------------------------------------------------


def resolve_static(req: RequestContext, repositories: list[str], namespaces: list[str] | None) -> None:
    """Mappings that configuration shows by itself: an allowlisted repository or a namespace with exactly the
    target's name. Anything weaker becomes a candidate, to be checked by evidence before it is used."""
    for t in req.targets:
        if t.kind == "repository" and t.mention in repositories:
            req.resolve("repository", t.mention, "named in the question; in the Git connector's allowlist")
        elif t.kind in ("domain", "name", "repository"):
            exact, near = match_repositories(t, repositories)
            if len(exact) == 1:
                req.resolve("repository", exact[0], f"the Git allowlist has a repository named {t.mention}")
            elif len(exact) > 1:
                req.leave_unresolved("repository", f"several allowlisted repositories are named {t.mention}", exact)
            elif repositories:
                req.leave_unresolved("repository", "", near)
        if namespaces is not None:
            exact_ns, near_ns = namespace_candidates(t, namespaces)
            if exact_ns:
                req.resolve("namespace", exact_ns[0], f"a namespace in scope is named {t.mention}")
            elif near_ns:
                req.leave_unresolved("namespace", "", near_ns)
    if req.targets and not req.resolved("repository") and "repository" not in req.unresolved and req.workflow == "guidance":
        req.leave_unresolved("repository", "no Git connector is configured, so no repository can be searched" if not repositories else "")
    if req.targets and not req.resolved("namespace") and "namespace" not in req.unresolved:
        req.leave_unresolved("namespace", "")


async def verify_namespace_by_ingress(inv: Any, executor: Any, req: RequestContext) -> None:
    """A domain maps to a namespace only when an Ingress there serves it. Candidates come from names; the
    host rule is what decides."""
    domain = next((t.mention.lower() for t in req.targets if t.kind == "domain"), None)
    candidates = req.candidates.get("namespace") or []
    if not domain or req.resolved("namespace") or "kubernetes" not in executor.ctx.connectors:
        return
    for ns in candidates[:MAX_INGRESS_LOOKUPS]:
        out = await inv._call(executor, "k8s_ingresses", {"namespace": ns}, f"does namespace {ns} serve {domain}?")
        if out is None or not out.ok:
            continue
        hosts = {str(r.get("host") or "").lower() for ing in (out.data or []) for r in (ing.get("rules") or [])}
        if domain in hosts or any(h.endswith("." + domain) for h in hosts):
            req.resolve("namespace", ns, f"an Ingress in {ns} serves {domain}", [out.evidence_ref])
            return
    if candidates:
        checked = ", ".join(candidates[:MAX_INGRESS_LOOKUPS])
        req.leave_unresolved("namespace", f"no Ingress in {checked} serves {domain}; other routing (an IngressRoute, a Gateway) may")


# ---- collecting sources ------------------------------------------------------------------------


def _rank_files(paths: list[str]) -> list[tuple[str, str]]:
    picked: list[tuple[int, int, str, str]] = []
    for p in paths:
        for rx, kind, weight in DEPLOY_FILES:
            if rx.search(p):
                picked.append((-weight, p.count("/"), p, kind))
                break
    picked.sort()
    out, used = [], {}
    for _, _, p, kind in picked:
        if used.get(kind, 0) < KIND_LIMIT.get(kind, 1):
            used[kind] = used.get(kind, 0) + 1
            out.append((p, kind))
    return out


def _mentions(text: str, req: RequestContext) -> bool:
    low = text.lower()
    names = [t.mention.lower() for t in req.targets] + ([req.repository.lower(), req.repository.split("/")[-1].lower()] if req.repository else [])
    return any(n and n in low for n in names)


def _source(inv: Any, *, ref: str, tool: str, path: str, repository: str | None, kind: str, text: str, freshness: dict | None, target_specific: bool, why: str,
            general: bool = False, authority: str | None = None, lines: list | None = None, revision: str | None = None) -> dict:
    row = {"ref": ref, "tool": tool, "path": path, "repository": repository, "kind": kind, "text": text, "freshness": freshness or {"state": "current"},
           "target_specific": target_specific, "general": general, "used": target_specific or general, "why": why, "authority": authority or ("target" if target_specific else None),
           "lines": lines, "revision": revision}
    inv.sources.append(row)
    inv.trace["sources"].append({k: v for k, v in row.items() if k != "text"})
    return row


async def _knowledge(inv: Any, executor: Any, req: RequestContext) -> list[dict]:
    """The index first: it may hold runbooks no repository connector reaches. A weak or empty result is not
    proof that nothing is written down, and a failed search is a different gap from an empty one. Relevance is
    the knowledge stage's: a mapping, the repository, the namespace, an identifier or the target's name make a
    passage the target's; a matching runbook with no service binding is general guidance, labeled as such."""
    from . import knowledge

    target = req.target_name or ""
    verb = "deploy" if req.primary in ("deployment_guidance", "execution") else ""
    queries = [q for q in dict.fromkeys([f"{verb} {target}".strip(), f"{target} deployment runbook rollback" if verb and target else ""]) if q]
    if not queries:
        queries = [req.question[:200]]
        extra = knowledge.context_query(inv, req.question)  # a follow-up such as "how do I fix it?" needs the turn before
        if extra:
            queries.append(extra)
    before = len(inv.knowledge["queries"])
    added = await knowledge.run(inv, executor, "initial", req.question, queries=queries[:2], purpose=f"documented {'deployment ' if verb else ''}steps for {target or 'this request'}")
    for q in inv.knowledge["queries"][before:]:
        if q["status"] == "failed":
            inv.gaps.append({"kind": "retrieval_failure", "detail": f"Knowledge search failed ({q.get('reason') or 'error'}); documented steps may exist but could not be read.", "evidence": [q["ref"]] if q.get("ref") else []})
        elif q["status"] == "ok" and q.get("evidence") in ("none", "weak"):
            inv.retrieval_notes.append(f"Knowledge search for {q['query']!r} returned {'nothing' if q.get('evidence') == 'none' else 'only loose matches'}; that does not show no documentation exists.")
    hits = {h.get("chunk_id"): h for e in inv.evidence if e["tool"] == "kb_search" and e["ok"] and isinstance(e.get("data"), list) for h in e["data"] if isinstance(h, dict)}
    found: list[dict] = []
    for p in added:
        hit = hits.get(p["chunk_id"]) or {}
        text = f"{hit.get('title') or ''}\n{hit.get('heading') or ''}\n{hit.get('text') or ''}"
        if any(s["path"] == p.get("path") and s["text"] == text for s in inv.sources):
            continue
        why = "; ".join(p["basis"]) if p["authority"] in ("target", "shared") else (p["excluded"] or "; ".join(p["basis"]))
        if p["authority"] == "unverified":
            why = f"not used for steps: {why}"
        found.append(_source(inv, ref=p["ref"], tool="kb_search", path=str(p.get("path") or ""), repository=p.get("repository"), kind="indexed", text=text, freshness=hit.get("freshness"),
                             target_specific=p["authority"] == "target", general=p["authority"] == "shared", authority=p["authority"], why=why, lines=p.get("lines"), revision=p.get("revision")))
    return found


async def _repository(inv: Any, executor: Any, req: RequestContext, budget_files: int) -> list[dict]:
    """The target's repository: list paths, then read the files a deployment is described in, most telling first.
    Stops once it has steps and has read one CI definition and one manifest, where the repository has them."""
    if "git" not in executor.ctx.connectors:
        return []
    repos = [req.repository] if req.repository else (req.candidates.get("repository") or [])[:MAX_DISCOVERY_REPOS]
    read: list[dict] = []
    for repo in repos:
        listed = await inv._call(executor, "git_list_files", {"repository": repo, "pattern": DISCOVERY_PATTERN, "limit": 300}, f"find deployment files in {repo}")
        if listed is None:
            continue
        if not listed.ok:
            inv.gaps.append({"kind": "retrieval_failure", "detail": f"Could not list files in {repo} ({listed.error_kind}: {(listed.error or '')[:160]}).", "evidence": [listed.evidence_ref] if listed.evidence_ref else []})
            continue
        files = _rank_files((listed.data or {}).get("paths") or [])
        if not files:
            inv.gaps.append({"kind": "missing_source", "detail": f"{repo} has no README, runbook, CI workflow, Dockerfile or manifest that MOKS recognizes.", "evidence": [listed.evidence_ref]})
        have_steps, seen_kinds = False, set()
        wanted = {k for _, k in files if k in ("ci", "manifest")}
        for path, kind in files:
            if len(read) >= budget_files or (have_steps and wanted <= seen_kinds and kind != "argocd"):
                inv.trace["sources"].append({"path": path, "repository": repo, "kind": kind, "used": False, "why": "not read: enough sources already, or the file budget is used"})
                continue
            out = await inv._call(executor, "git_read_file", {"repository": repo, "path": path}, f"{kind} for {req.target_name or repo}")
            if out is None:
                continue
            if not out.ok:
                inv.gaps.append({"kind": "retrieval_failure", "detail": f"Could not read {repo}:{path} ({out.error_kind}).", "evidence": [out.evidence_ref] if out.evidence_ref else []})
                continue
            text = str((out.data or {}).get("text") or "")
            confirmed = bool(req.repository) or _mentions(text, req)
            if not req.repository and confirmed:
                req.resolve("repository", repo, f"{repo}:{path} names {req.target_name}", [out.evidence_ref])
            src = _source(inv, ref=out.evidence_ref, tool="git_read_file", path=path, repository=repo, kind=kind, text=text, freshness={"state": "current", "revision": (out.data or {}).get("ref")},
                          target_specific=confirmed, why="in the target's repository" if confirmed else f"in {repo}, which is only a candidate for {req.target_name}")
            read.append(src)
            extracted = extract([src])
            have_steps = have_steps or bool(extracted["steps"])
            seen_kinds.add(kind)
        if req.repository:
            break
    return read


def _manifests(sources: list[dict]) -> tuple[list[tuple[str, str]], list[dict]]:
    """Namespaces the target's manifests declare, and Argo CD Applications they define (name, destination)."""
    namespaces: list[tuple[str, str]] = []
    apps: list[dict] = []
    for s in sources:
        if not s["target_specific"] or not re.search(r"\.ya?ml$", s["path"], re.I) or s["tool"] != "git_read_file":
            continue
        try:
            docs = [d for d in yaml.safe_load_all(s["text"]) if isinstance(d, dict)]
        except yaml.YAMLError:
            continue
        for d in docs:
            meta = d.get("metadata") or {}
            if d.get("kind") == "Namespace" and meta.get("name"):
                namespaces.append((str(meta["name"]), s["path"]))
            elif str(d.get("apiVersion", "")).startswith("argoproj.io/") and d.get("kind") == "Application" and meta.get("name"):
                dest = (d.get("spec") or {}).get("destination") or {}
                apps.append({"name": str(meta["name"]), "namespace": dest.get("namespace"), "path": s["path"], "ref": s["ref"]})
            elif d.get("kind") == "Kustomization" or s["path"].endswith(("kustomization.yaml", "kustomization.yml")):
                if d.get("namespace"):
                    namespaces.append((str(d["namespace"]), s["path"]))
            elif meta.get("namespace"):
                namespaces.append((str(meta["namespace"]), s["path"]))
    return namespaces, apps


async def gather(inv: Any, executor: Any, req: RequestContext) -> None:
    """Sources first, then the few live reads that answer something about the target."""
    await _knowledge(inv, executor, req)
    if "git" in executor.ctx.connectors and (req.repository or req.candidates.get("repository")):
        await _repository(inv, executor, req, inv.settings.guidance_max_files)
    elif req.targets and "git" not in executor.ctx.connectors:
        inv.gaps.append({"kind": "missing_source", "detail": "No Git connector is configured for this environment, so repository files (README, CI, manifests) could not be read.", "evidence": []})
    declared, apps = _manifests(inv.sources)
    if declared and not req.resolved("namespace"):
        names = sorted({n for n, _ in declared})
        if len(names) == 1:
            path = next(p for n, p in declared if n == names[0])
            req.resolve("namespace", names[0], f"the target's manifests declare it ({path})", [s["ref"] for s in inv.sources if s["path"] == path][:1])
        else:
            req.leave_unresolved("namespace", f"the target's manifests name several namespaces ({', '.join(names[:5])})", names)
    if not req.resolved("namespace"):
        await verify_namespace_by_ingress(inv, executor, req)
    if req.namespaces:
        req.scope = "target"
    ctx = executor.ctx
    ns = req.namespaces[0] if req.namespaces else None
    if ns and "kubernetes" in ctx.connectors and ctx.scope.allows(ns):
        if "troubleshooting" in req.intents or "health_check" in req.intents:
            await inv._call(executor, "k8s_triage", {"namespace": ns}, f"the question also asks about current problems in {ns}")
        else:
            await inv._call(executor, "k8s_workloads", {"namespace": ns}, f"what runs in {ns} now: the starting point for an upgrade or a rollback")
    for app in apps[:1]:
        if "kubernetes" in ctx.connectors and ctx.scope.cluster_wide:
            out = await inv._call(executor, "k8s_argocd_status", {"application": app["name"]}, f"status of Argo CD app {app['name']}, which the target's repository defines")
            if out is not None and out.ok and not (out.data or {}).get("total"):
                inv.notes.append(f"{app['path']} defines Argo CD Application {app['name']}, but no such Application exists in this cluster.")


# ---- extracting guidance -----------------------------------------------------------------------


def _sections(text: str) -> list[tuple[str, int, list[str]]]:
    """(heading, level, body lines). The text before the first heading is a section with an empty heading."""
    out: list[tuple[str, int, list[str]]] = [("", 0, [])]
    fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fence = not fence
        m = None if fence else _HEADING.match(line)
        if m:
            out.append((m.group(2).strip(), len(m.group(1)), []))
        else:
            out[-1][2].append(line)
    return out


def _category(heading: str) -> str | None:
    for name in ("rollback", "verification", "prerequisites", "steps"):
        if _KIND_OF_HEADING[name].search(heading):
            return name
    return None


def _item(text: str, src: dict, command: str | None = None, note: str | None = None) -> dict:
    body = f"{command or ''} {text}"
    return {"text": text.strip()[:400], "command": command, "note": note, "sources": [src["ref"]], "path": src["path"], "repository": src.get("repository"),
            "support": "quoted", "placeholders": sorted(set(_PLACEHOLDER.findall(body)))[:6], "general": bool(src.get("general"))}


def _paragraphs(lines: list[str]) -> list[str]:
    paras, cur, fence = [], [], False
    for line in lines:
        if _FENCE.match(line):
            fence = not fence
            continue
        if fence or _BULLET.match(line) or line.strip().startswith("|"):
            if cur:
                paras.append(" ".join(cur))
                cur = []
            continue
        if line.strip():
            cur.append(line.strip())
        elif cur:
            paras.append(" ".join(cur))
            cur = []
    if cur:
        paras.append(" ".join(cur))
    return paras


def _markdown(src: dict, out: dict) -> None:
    secs = _sections(src["text"])
    for heading, level, lines in secs:
        cat = _category(heading)
        if level <= 1 and not out["overview"] and src.get("target_specific"):  # a shared runbook does not describe the target
            para = next((p for p in _paragraphs(lines) if len(p) > 40), None)
            if para:
                out["overview"].append({"text": para[:600], "sources": [src["ref"]], "path": src["path"]})
        if cat is None:
            continue
        fence = False
        for line in lines:
            f = _FENCE.match(line)
            if f:
                fence = not fence
                continue
            if fence:
                raw = line.strip()
                if not raw or raw.startswith("#"):
                    continue
                raw = re.sub(r"^\$\s+", "", raw)
                cmd, _, comment = raw.partition(" #")
                cmd, comment = cmd.strip(), comment.strip()
                out[cat].append(_item(comment or f"Run `{cmd}`", src, cmd, comment or None))
                if cat != "rollback" and comment and _ROLLBACK_WORDS.search(comment):
                    out["rollback"].append(_item(comment, src, cmd, comment))
                continue
            b = _BULLET.match(line)
            if b and b.group(1).strip():
                out[cat].append(_item(b.group(1), src))
        for para in _paragraphs(lines):
            for sentence in ev.sentences(para):
                if cat != "verification" and _VERIFY_WORDS.search(sentence):
                    out["verification"].append(_item(sentence, src))
                if cat != "rollback" and _ROLLBACK_WORDS.search(sentence):
                    out["rollback"].append(_item(sentence, src))
            if cat in ("steps", "prerequisites") and len(para) > 30:
                out[cat].append(_item(para[:400], src))


def _ci(src: dict, out: dict) -> None:
    try:
        doc = yaml.safe_load(src["text"])
    except yaml.YAMLError:
        out["configuration"].append({"path": src["path"], "kind": src["kind"], "detail": "CI definition (could not be parsed as YAML)", "sources": [src["ref"]]})
        return
    if not isinstance(doc, dict):
        return
    trig = doc.get("on", doc.get(True))  # YAML reads a bare `on:` as true
    trig_text = ", ".join(trig if isinstance(trig, list) else list(trig) if isinstance(trig, dict) else [str(trig)]) if trig else "unknown triggers"
    jobs = doc.get("jobs") or {}
    runs = []
    for job in (jobs.values() if isinstance(jobs, dict) else []):
        for step in (job or {}).get("steps") or []:
            run = str((step or {}).get("run") or "")
            runs += [ln.strip() for ln in run.splitlines() if ln.strip() and _DEPLOY_LINE.search(ln)]
    out["configuration"].append({"path": src["path"], "kind": "ci", "sources": [src["ref"]],
                                 "detail": f"CI workflow {doc.get('name') or src['path']}: runs on {trig_text}; jobs {', '.join(list(jobs)[:6]) or 'none'}"
                                           + (f"; deploy-related commands: {'; '.join(runs[:4])}" if runs else "")})


def _other(src: dict, out: dict) -> None:
    text, kind = src["text"], src["kind"]
    if kind == "dockerfile":
        bases = re.findall(r"(?im)^FROM\s+(\S+)", text)
        out["configuration"].append({"path": src["path"], "kind": kind, "detail": "Image build" + (f" from {', '.join(bases[:3])}" if bases else ""), "sources": [src["ref"]]})
    elif kind == "script":
        header = [ln.lstrip("# ").strip() for ln in text.splitlines()[1:12] if ln.startswith("#") and ln.lstrip("# ").strip()]
        out["configuration"].append({"path": src["path"], "kind": kind, "detail": "Script" + (f": {' '.join(header[:3])[:300]}" if header else ""), "sources": [src["ref"]]})
    elif kind in ("manifest", "argocd", "helm"):
        out["configuration"].append({"path": src["path"], "kind": kind, "detail": {"manifest": "Kubernetes manifests", "argocd": "Argo CD definition", "helm": "Helm chart"}[kind], "sources": [src["ref"]]})


def extract(sources: list[dict]) -> dict[str, Any]:
    """Guidance by rules: only what the sources say, each item citing where it says it."""
    out: dict[str, Any] = {"overview": [], "configuration": [], **{s: [] for s in SECTIONS}}
    for src in sources:
        if not src.get("target_specific") and not src.get("general"):
            continue
        if src["tool"] == "kb_search" or src["path"].lower().endswith((".md", ".markdown", ".txt", ".rst")) or src["kind"] in ("readme", "runbook"):
            _markdown(src, out)
        elif src["kind"] == "ci":
            _ci(src, out)
        else:
            _other(src, out)
    for s in ("verification", "rollback"):  # prose that only mentions it adds nothing beside a cited command from the same file
        commanded = {i["path"] for i in out[s] if i["command"]}
        out[s] = [i for i in out[s] if i["command"] or i["path"] not in commanded]
    for s in SECTIONS:
        seen, keep = set(), []
        for item in out[s]:
            key = (item["command"] or item["text"]).lower()
            if key not in seen:
                seen.add(key)
                keep.append(item)
        out[s] = keep[:12]
    return out


def conflicts(sources: list[dict], extracted: dict) -> list[dict]:
    """Where the sources disagree or may be out of date. Marked for a person; MOKS does not pick a winner."""
    out = []
    for s in sources:
        fr = s.get("freshness") or {}
        mine = s["target_specific"] or s.get("general")
        if mine and fr.get("state") not in (None, "current"):
            out.append({"kind": "stale", "detail": f"{s['path']} is {fr['state']}" + (f" ({fr.get('reason')})" if fr.get("reason") else "") + "; check what replaced it before following it.", "sources": [s["ref"]]})
        if mine and fr.get("possible_conflict"):
            out.append({"kind": "conflict", "detail": f"{s['path']} may disagree with {', '.join(fr.get('conflict_with') or ['another document'])}.", "sources": [s["ref"]]})
    by_path: dict[str, set[str]] = {}
    for item in extracted["steps"]:
        if item["command"]:
            by_path.setdefault(item["path"], set()).add(item["command"])
    if len(by_path) > 1:
        paths = sorted(by_path)
        out.append({"kind": "conflict", "detail": f"{len(paths)} sources describe deployment commands ({', '.join(paths[:4])}); check which one is current.",
                    "sources": sorted({r for i in extracted["steps"] if i["command"] for r in i["sources"]})})
    return out


def _gaps(inv: Any, req: RequestContext, extracted: dict) -> list[dict]:
    gaps = list(inv.gaps)
    specific = [s for s in inv.sources if s["target_specific"]]
    general = [s for s in inv.sources if s.get("general")]
    if req.targets and not specific and general:
        gaps.append({"kind": "insufficient_evidence", "detail": f"Only general runbook guidance was found ({', '.join(sorted({s['path'] for s in general})[:3])}); nothing written for {req.target_name} itself.",
                     "evidence": sorted({s["ref"] for s in general})[:4]})
    if req.targets and not specific and not any(g["kind"] == "retrieval_failure" for g in gaps):
        where = []
        if any(s["tool"] == "kb_search" for s in inv.sources) or any(t["tool"] == "kb_search" for t in inv.trace["tools"]):
            where.append("the knowledge index")
        if req.repository:
            where.append(req.repository)
        elif req.candidates.get("repository"):
            where.append(" and ".join(req.candidates["repository"][:MAX_DISCOVERY_REPOS]) + " (candidates)")
        gaps.append({"kind": "missing_source", "detail": f"No source about {req.target_name} was found in {', '.join(where) or 'any searched location'}.", "evidence": []})
    elif specific and not [i for i in extracted["steps"] if not i.get("general")] and req.primary in ("deployment_guidance", "execution"):
        gaps.append({"kind": "insufficient_evidence", "detail": f"{len(specific)} source(s) about {req.target_name or 'the target'} were read, but none describes deployment steps.", "evidence": [s["ref"] for s in specific][:4]})
    if specific and req.primary in ("deployment_guidance", "execution"):
        for section, label in (("verification", "how to verify a deployment"), ("rollback", "how to roll back")):
            if not extracted[section]:
                gaps.append({"kind": "insufficient_evidence", "detail": f"The sources do not say {label}.", "evidence": []})
    for field in req.unresolved:
        if field == "target":
            gaps.append({"kind": "unresolved", "detail": "The question does not name what to deploy.", "evidence": []})
        elif field == "repository" and req.targets:
            cands = req.candidates.get("repository")
            gaps.append({"kind": "unresolved", "detail": f"No allowlisted repository is confirmed to hold {req.target_name}" + (f" (candidates: {', '.join(cands)})" if cands else "") + ".", "evidence": []})
        elif field == "namespace" and req.targets:
            cands = req.candidates.get("namespace")
            gaps.append({"kind": "unresolved", "detail": f"The namespace {req.target_name} runs in is not established" + (f" (candidates: {', '.join(cands)})" if cands else "") + ".", "evidence": []})
    for a in req.ambiguities:
        if a and not any(a in g["detail"] for g in gaps):
            gaps.append({"kind": "unresolved", "detail": a[0].upper() + a[1:] + ".", "evidence": []})
    return gaps


def _question_for_user(req: RequestContext, gaps: list[dict], used: list[dict]) -> str | None:
    """One focused question, only when the answer cannot go further without it."""
    if "target" in req.unresolved:
        return "Which application, repository or domain do you want to deploy?"
    if used:
        return None
    if req.targets and "repository" in req.unresolved and len(req.candidates.get("repository") or []) > 1:
        return f"Which repository holds {req.target_name}: {' or '.join(req.candidates['repository'][:3])}?"
    if req.targets and all(g["kind"] in ("missing_source", "unresolved") for g in gaps) and gaps and not req.repository:
        return f"Where is {req.target_name} deployed from (repository, or the runbook that describes it)?"
    return None


def _execution(inv: Any, req: RequestContext) -> dict[str, Any]:
    """What a request to change something gets: a scoped plan and the route that authorizes changes. Nothing runs."""
    from ..actions.catalog import CATALOG

    env = inv.environment_info
    verbs = [v for v in ("deploy", "restart", "scale", "rollback", "delete", "sync", "patch") if v in req.question.lower()]
    requested = verbs[0] if verbs else "change"
    available = {"deploy": ["open_pull_request", "rollback_deployment"], "rollback": ["rollback_deployment"], "restart": ["restart_deployment"], "scale": ["scale_deployment"],
                 "delete": ["delete_pod"], "patch": ["apply_config_patch", "patch_resources"], "sync": ["open_pull_request"]}.get(requested, [])
    actions = [{"action": a, "title": CATALOG[a].title} for a in available if a in CATALOG]
    named = requested in CATALOG or f"{requested}_deployment" in CATALOG
    return {
        "requested": requested, "executed": False, "target": req.target_name, "environment": env.get("name"), "production": env.get("is_production"),
        "requested_environment": req.requested_environment, "namespace": req.namespaces[0] if req.namespaces else None,
        "allowlisted": named, "actions": actions,
        "authorization": [
            "Request an action plan for the exact target (needs action.request in this environment and namespace).",
            "Review its preconditions, impact, blast radius and rollback.",
            "In production a second person with action.approve approves it; a requester cannot approve their own plan.",
            "Execution is validated afterwards, and recovery runs if validation fails.",
        ],
        "note": (f"MOKS has no allowlisted {requested} action, so it cannot {requested} {req.target_name or 'this'} itself. The steps below come from the target's sources and are for a person to run, "
                 "or to turn into a reviewed change (for example a pull request) through an action plan.") if not named else
                (f"{requested} is an allowlisted action: start it as an action plan for review. MOKS has not run it."),
    }


def _current_state(inv: Any, req: RequestContext) -> list[Fact]:
    facts = []
    for e in inv.evidence:
        if e["ok"] and e["tool"] in ("k8s_workloads", "k8s_triage", "k8s_argocd_status") and req.in_scope(e["args"].get("namespace")) is not False:
            if e["tool"] == "k8s_argocd_status" and not e["args"].get("application"):
                continue
            facts.append(Fact(statement=f"{e['tool']} ({', '.join(f'{k}={v}' for k, v in e['args'].items())}): {e['summary']}", evidence=[e["ref"]]))
    return facts


def _summary(req: RequestContext, used: list[dict], g: dict, gaps: list[dict], execution: dict | None) -> str:
    parts = []
    if execution:
        parts.append(execution["note"].split(". ")[0] + ".")
    if used:
        paths = sorted({s["path"] for s in used})
        what = {"deployment_guidance": "Deployment guidance", "execution": "Deployment plan", "repository": "Answer"}.get(req.primary, "Answer")
        more = f" and {len(paths) - 4} more" if len(paths) > 4 else ""
        parts.append(f"{what} for {req.target_name or 'this request'} from {len(paths)} source(s): {', '.join(paths[:4])}{more}.")
        if g["overview"]:
            parts.append(g["overview"][0]["text"][:300].rstrip(".") + ".")
    if gaps:
        parts.append("Not established: " + "; ".join(x["detail"].rstrip(".") for x in gaps[:3]) + ".")
    return " ".join(parts) or "MOKS found nothing to answer this with."


def _command_supported(command: str, refs: list[str], texts: dict[str, str]) -> bool:
    norm = lambda s: re.sub(r"\s+", " ", s).strip().lower()  # noqa: E731
    c = norm(command)
    return bool(c) and any(c in norm(texts.get(r, "")) for r in refs)


def _validate_llm(raw: dict, rules: dict, texts: dict[str, str], doc_refs: set[str]) -> tuple[dict, list[str]]:
    """A model's guidance keeps an item only if it cites a source MOKS read, and keeps a command only if that
    command occurs in what it cites. Where nothing of a section survives, the rule-based section stands."""
    dropped: list[str] = []
    out: dict[str, Any] = {"overview": rules["overview"], "configuration": rules["configuration"]}
    for section in SECTIONS:
        kept = []
        for item in (raw.get(section) or [])[:12]:
            if not isinstance(item, dict) or not str(item.get("text") or "").strip():
                continue
            refs = [r for r in item.get("sources") or [] if r in doc_refs]
            if not refs:
                dropped.append(f"{section}: {str(item.get('text'))[:120]} (cites no source MOKS read)")
                continue
            cmd = str(item.get("command") or "").strip() or None
            support = "quoted"
            if cmd and not _command_supported(cmd, refs, texts):
                dropped.append(f"{section}: command `{cmd[:120]}` does not occur in {', '.join(refs)}")
                cmd, support = None, "explained"
            body = f"{cmd or ''} {item['text']}"
            kept.append({"text": str(item["text"])[:400], "command": cmd, "note": None, "sources": refs, "path": None, "repository": None, "support": support,
                         "placeholders": sorted(set(_PLACEHOLDER.findall(body)))[:6]})
        out[section] = kept if kept else rules[section]
    return out, dropped


async def compose(inv: Any, provider: Any, cfg: Any, req: RequestContext) -> tuple[Report, str]:
    """The guidance answer, as a Report whose `answer` carries the structure the request calls for."""
    used = [s for s in inv.sources if s["target_specific"] or s.get("general")]
    rules = extract(used)
    gaps = _gaps(inv, req, rules)
    execution = _execution(inv, req) if req.primary == "execution" else None
    g, generated, notes = rules, "rules", []
    summary = _summary(req, used, rules, gaps, execution)
    if provider and used:
        texts = {s["ref"]: s["text"] for s in used}
        try:
            raw = await inv._llm(provider, cfg, prompts.GUIDANCE_SYSTEM, prompts.guidance_user(req, used, rules, gaps), 1600, 400)
            g, dropped = _validate_llm(raw, rules, texts, set(texts))
            generated = "llm"
            if dropped:
                notes.append(f"{len(dropped)} item(s) of the drafted guidance were removed: " + "; ".join(dropped[:4]))
            if str(raw.get("summary") or "").strip():
                summary = (execution["note"].split(". ")[0] + ". " if execution else "") + str(raw["summary"])[:1200]
        except inv.llm_failures as exc:
            llm_answer_unused("guidance", exc)
            notes.append(f"LLM guidance unavailable ({getattr(exc, 'message', str(exc))}); showing guidance extracted from the sources.")
    answer = {**g, "conflicts": conflicts(used, g), "gaps": gaps, "assumptions": [], "question_for_user": _question_for_user(req, gaps, [s for s in used if s["target_specific"]]),
              "sources": [{k: v for k, v in s.items() if k != "text"} for s in inv.sources], "retrieval": inv.retrieval_notes,
              "target": {"mention": req.target_name, "repository": req.repository, "namespaces": req.namespaces, "environment": req.environment, "cluster": req.cluster,
                         "resolutions": [r.model_dump() for r in req.resolutions], "unresolved": req.unresolved, "candidates": req.candidates}}
    if any(s.get("general") for s in used):
        answer["assumptions"].append("Items marked general come from shared runbooks that are not written for this target: check they apply before following them.")
    if req.requested_environment and req.environment and req.requested_environment.startswith("prod") and not inv.environment_info.get("is_production"):
        answer["assumptions"].append(f"You asked about production; this answer is for environment {req.environment}, which is not marked as production.")
    for item in (x for s in SECTIONS for x in g[s]):
        for p in item["placeholders"]:
            note = f"`{p}` is a placeholder: replace it with a real value."
            if note not in answer["assumptions"]:
                answer["assumptions"].append(note)
    if execution:
        answer["execution"] = execution
    rep = Report(summary=summary[:2000], observed_facts=_current_state(inv, req), missing_evidence=[x["detail"] for x in gaps][:12],
                 next_steps=[answer["question_for_user"]] if answer["question_for_user"] else [], generated_by=generated, notes=[*inv.notes, *notes, *inv.retrieval_notes],
                 response_type=req.response_type, answer=answer)
    return rep, ("deterministic" if not provider else "llm" if generated == "llm" else "llm_fallback")


# ---- concepts ----------------------------------------------------------------------------------


async def explain(inv: Any, executor: Any, provider: Any, cfg: Any, req: RequestContext) -> tuple[Report, str]:
    """A direct answer. Documentation may be cited; nothing live is read."""
    await _knowledge(inv, executor, req)
    docs = [s for s in inv.sources if s.get("authority") in ("target", "shared", "unverified")]
    for s in docs:
        s["used"] = True  # a concept is not tied to a target, so any relevant readable passage may help
    refs = {s["ref"] for s in docs}
    answer: dict[str, Any] = {"sources": [{k: v for k, v in s.items() if k != "text"} for s in docs], "gaps": list(inv.gaps), "retrieval": inv.retrieval_notes}
    if provider:
        try:
            raw = await inv._llm(provider, cfg, prompts.CONCEPT_SYSTEM, prompts.concept_user(req, docs), 1200, 300)
            text = str(raw.get("answer") or "").strip()
            cited = [r for r in raw.get("sources") or [] if r in refs]
            if text:
                answer["explanation"] = text[:4000]
                answer["cited"] = cited
                answer["examples"] = [str(x)[:400] for x in (raw.get("examples") or [])[:4] if str(x).strip()]
                return Report(summary=text[:2000], generated_by="llm", notes=list(inv.notes), response_type=req.response_type, answer=answer), "llm"
        except inv.llm_failures as exc:
            llm_answer_unused("concept", exc)
            inv.notes.append(f"LLM answer unavailable ({getattr(exc, 'message', str(exc))}).")
    passages = [s for s in docs if s["text"].strip()][:3]
    if passages:
        answer["explanation"] = "\n\n".join(f"> {s['text'].strip()[:500]}\n\n— {s['path']}" for s in passages)
        answer["cited"] = [s["ref"] for s in passages]
        summary = f"From the documentation MOKS can read ({', '.join(sorted({s['path'] for s in passages}))}). A chat model would explain this in its own words; none answered here."
    else:
        summary = "MOKS answers conceptual questions with a chat model, and none is available here; the knowledge index has nothing readable on this either."
        answer["gaps"].append({"kind": "missing_source", "detail": "No readable documentation matched, and no chat model answered.", "evidence": []})
    return Report(summary=summary, generated_by="rules", notes=list(inv.notes), response_type=req.response_type, answer=answer), "deterministic" if not provider else "llm_fallback"


def sources_digest(sources: list[dict], per_item: int, total: int) -> str:
    parts, used = [], 0
    for s in sources:
        head = (f"[{s['ref']}] {s['repository'] or ''}:{s['path']} kind={s['kind']} freshness={(s.get('freshness') or {}).get('state')}"
                + (" GENERAL GUIDANCE: a shared runbook, not written for this target" if s.get("general") else ""))
        chunk = head + "\n" + prompts.untrusted(s["path"] or s["tool"], s["text"], per_item)
        if used + len(chunk) > total:
            parts.append(head + " (omitted for length)")
            continue
        parts.append(chunk)
        used += len(chunk)
    return "\n\n".join(parts) or "(no sources)"


def as_json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))
