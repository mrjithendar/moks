"""Knowledge retrieval as a stage of every investigation, not something a planner has to think of.

Before live evidence is read, the request is searched against the knowledge index with the original question
and the context MOKS resolved (environment, namespace, service, resource, repository). After triage, one
bounded refresh runs when the findings name symptoms the first queries did not. Guidance, concept answers and
follow-up turns use the same stage, so every answer records what was searched, what came back and why each
passage was or was not used.

Every search is a kb_search tool call: the executor applies RBAC, the person's grants and the request's
environment and namespace before ranking, stores the result as evidence and counts it against the budget.
Relevance is decided afterwards, on passages the person may already read, so it can only narrow.

A passage gets one authority:
  target        explicitly tied to what is being asked about (service mapping, repository, a resource
                identifier, or it names the target), with no explicit mapping to another service
  dependency    declared by its source as documentation for something the target depends on
  shared        general documentation with no service binding that matches the request: general guidance
  unverified    relevant, but from a repository whose relationship to the target is not established
  unrelated     written for another service: kept out of the answer
  low_relevance shares too little with the request to help
Retrieved text is data. It reaches a model only inside untrusted blocks, never adds tools or scope, and a
runbook describes intent, never the current state of anything.

Every passage has its own citation id, `E3.2`: the kb_search evidence it came from and its rank in that search.
One search returns several passages, so citing `E3` alone cannot say which one an answer relied on. A passage
moves through four states, each a subset of the one before:
  retrieved   returned by a search the person was allowed to run
  eligible    its authority lets it inform the answer (target, shared, unverified)
  supplied    its text was put in front of the model within the prompt budget (rules reports supply nothing)
  cited       the final answer cites its id, and the citation survived claim validation
A model may only cite what it was supplied; a citation of anything else is recorded as rejected.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from ..observability import KNOWLEDGE_RETRIEVALS
from ..rag.chunking import kind_of
from ..redaction import redact

log = logging.getLogger("moks.orchestrator.knowledge")

K = 6
MAX_INITIAL = 2
MAX_REFRESH = 1
MIN_SECONDS_LEFT = 5          # a search started this close to the time limit would only take time from the report
PROMPT_PASSAGE_CHARS = 1200
PROMPT_TOTAL_CHARS = 9000
EXCERPT_CHARS = 300
LEXICAL = 0.34                # share of the request's terms a passage must contain to count as relevant
SEMANTIC_FLOOR = 0.15         # with vectors, a top-ranked passage needs only this much literal overlap
USABLE = ("target", "dependency", "shared", "unverified")
STATES = ("retrieved", "eligible", "supplied", "cited")
WORD = re.compile(r"[a-z0-9][a-z0-9-]{2,}")
STOP = set("the and for are was why how what when with from that this does did has have not can you our into any all its show get "
           "there their them they then than which while about after before again also been being here just more most some such very "
           "will would should could please tell me is it in on of to a an be do".split())

# Finding kinds as the words runbooks use for them, so a refresh query finds the section about the symptom.
SYMPTOM_WORDS = {
    "crashloop": "CrashLoopBackOff crash loop restart", "oomkilled": "OOMKilled memory limit", "oomkilled_earlier": "OOMKilled memory limit",
    "image_pull": "ImagePullBackOff image pull registry", "config_error": "CreateContainerConfigError missing secret configmap",
    "unschedulable": "pod unschedulable insufficient resources", "pending": "pod pending scheduling", "not_ready": "readiness probe failing not ready",
    "evicted": "pod evicted node pressure", "workload_unavailable": "deployment unavailable replicas", "argocd_comparison_error": "Argo CD ComparisonError sync",
    "service_no_endpoints": "service no endpoints selector", "probe_failure": "probe failure liveness readiness",
}

REASONS = {
    "not_attempted": "Search was not attempted.",
    "budget": "Search was not attempted: the knowledge stage's search allowance was already used.",
    "time": "Search was not attempted: the investigation was too close to its time limit.",
    "out_of_request": "Search was not attempted: this request does not use the knowledge index.",
    "failed": "Knowledge search failed; documented procedures may exist but could not be read.",
    "none": "No relevant authorized passages were found for this request.",
}


def new_state() -> dict[str, Any]:
    return {"status": "not_attempted", "stages": [], "scope": {}, "queries": [], "passages": [], "reasons": [], "hints": [], "calls": 0}


def terms(text: str) -> set[str]:
    return {w for w in WORD.findall(text.lower()) if w not in STOP}


def _overlap(wanted: set[str], hay: str) -> float:
    return sum(1 for t in wanted if t in hay) / len(wanted) if wanted else 0.0


# ---- what the request is about ------------------------------------------------------------------


def context(inv: Any, question: str) -> dict[str, Any]:
    """Names, identifiers and scope that tie a passage to this request. From MOKS's own resolution and the
    run's findings, never from retrieved text."""
    req = inv.request
    names: set[str] = set()
    repository = req.repository if req else None
    namespaces = set(req.namespaces) if req else set()
    if inv.focus_namespace and inv.focus_namespace != "*":
        namespaces.add(inv.focus_namespace)
    for t in (req.targets if req else []):
        if t.kind != "object":
            names.add(t.mention.lower())
            names.add(t.mention.lower().split(".")[0])  # jithendar.com is also written jithendar
    if repository:
        names.add(repository.split("/")[-1].lower())
    identifiers: set[str] = set()
    for f in inv.findings:
        if req is not None and f.get("namespace") and not req.in_scope(f.get("namespace")):
            continue
        for r in f.get("resources") or []:
            name = str(r).partition("/")[2].lower()
            if len(name) >= 4:
                identifiers.add(name)
                base = _workload(name)
                if base:
                    names.add(base)
        if f.get("controller"):
            names.add(str(f["controller"]).partition("/")[2].lower())
    names.discard("")
    words = terms(question)
    if inv.history and len(words) < 6:  # a short follow-up is about what the turn before it asked
        words |= terms(_last_question(inv))
    return {"names": {n for n in names if len(n) >= 3}, "repository": repository, "namespaces": namespaces, "identifiers": identifiers,
            "terms": words, "environment": getattr(getattr(inv, "request", None), "environment", None)}


def _last_question(inv: Any) -> str:
    return next((h["text"] for h in reversed(inv.history) if h.get("from") != "MOKS"), "")


def _workload(pod: str) -> str | None:
    """cart-7f9c-xyz -> cart. A guess about naming, used only to rank, never to authorize."""
    parts = pod.split("-")
    return "-".join(parts[:-2]) if len(parts) >= 3 else None


def context_query(inv: Any, question: str) -> str | None:
    """The second query of initial_queries: the request named by what MOKS resolved and the turn before."""
    rest = initial_queries(inv, question)[1:]
    return rest[0] if rest else None


def initial_queries(inv: Any, question: str) -> list[str]:
    """The question as asked, then the same request named by what MOKS resolved, when that adds anything."""
    req = inv.request
    q1 = " ".join(question.split())[:300]
    extra: list[str] = []
    if req is not None:
        if req.target_name:
            extra.append(req.target_name)
        if req.repository:
            extra.append(req.repository.split("/")[-1])
        extra += [n for n in req.namespaces[:2]]
    if inv.focus_namespace and inv.focus_namespace != "*":
        extra.append(inv.focus_namespace)
    if inv.history and len(terms(question)) < 6:  # "what about the logs?" means nothing without the turn before it
        extra += sorted(terms(_last_question(inv)) - terms(question))[:6]
    words = list(dict.fromkeys(w for w in extra if w))
    q2 = " ".join([*words, "runbook troubleshooting"]) if words else ""
    out = [q1] if len(q1) >= 2 else []
    if q2 and terms(q2) - terms(q1) - {"runbook", "troubleshooting"}:
        out.append(q2[:300])
    return out[:MAX_INITIAL]


def refresh_query(inv: Any) -> str | None:
    """One query from what triage found, when it names symptoms the earlier queries did not."""
    req = inv.request
    mine = [f for f in inv.findings if req is None or req.in_scope(f.get("namespace")) or f.get("namespace") is None]
    if not mine:
        return None
    ranked = sorted(mine, key=lambda f: {"high": 0, "medium": 1, "low": 2}.get(f.get("severity"), 3))
    words: list[str] = []
    for f in ranked[:3]:
        words.append(SYMPTOM_WORDS.get(f["kind"], f["kind"].replace("_", " ")))
        if f["kind"] == "crash_log_reason":
            words.append(" ".join(sorted(terms(str(f.get("title", "")).partition("after logging:")[2]))[:8]))
    cx = context(inv, inv.request.question if inv.request else "")
    words += sorted(cx["names"])[:3]
    q = " ".join(dict.fromkeys(" ".join(words).split()))[:300]
    asked = set().union(*(terms(x["query"]) for x in inv.knowledge["queries"])) if inv.knowledge["queries"] else set()
    new = terms(q) - asked
    if len(new) < 2 or len(new) < 0.4 * max(len(terms(q)), 1):
        return None  # the findings add nothing the first search did not already look for
    return q


# ---- relevance ------------------------------------------------------------------------------------


def classify(hit: dict, cx: dict, rank: int, mode: str | None) -> tuple[str, list[str], str | None]:
    """(authority, basis, why it is left out). Runs on passages the person may already read: relevance only
    narrows what authorization allowed and never stands in for it.

    Order matters. Explicit relationships are decided first: the source's service mapping, a declared
    dependency, and a mapping to some other service (a conflict). Weaker signals (the repository's name,
    identifiers, the target's name in the text) count only when nothing explicit contradicts them. A namespace
    is supporting context: several services share one, so a namespace match alone never makes a passage the
    target's."""
    cit, scope = hit.get("citation") or {}, hit.get("scope") or {}
    applies = scope.get("applies_to") or {}
    services = {str(s).lower() for s in [scope.get("service"), *(applies.get("services") or [])] if s}
    dependents = {str(s).lower() for s in applies.get("dependency_of") or [] if s}  # documents a dependency these services use
    declared_shared = bool(applies.get("shared"))  # the source owner says it applies to every service in its scope
    repo = str(cit.get("repository") or "")
    short = repo.split("/")[-1].lower() if repo else ""
    ns = scope.get("namespace") or "*"
    path = str(cit.get("path") or "")
    hay = f"{hit.get('title') or ''} {hit.get('heading') or ''} {hit.get('text') or ''} {path}".lower()
    names = cx["names"]
    support = [f"scoped to namespace {ns} (supporting context only: other services may share it)"] if ns != "*" and ns in cx["namespaces"] else []
    lexical = _overlap(cx["terms"], hay)
    share = f"shares {round(lexical * 100)}% of the request's terms"
    relevant = lexical >= LEXICAL or (mode in ("hybrid", "vector") and rank < 3 and lexical >= SEMANTIC_FLOOR)
    mentioned = sorted(n for n in names if re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", hay))

    # 1. explicit relationships
    if services & names:
        return "target", [f"source maps it to service {', '.join(sorted(services & names))}", *support], None
    if services & cx["terms"] and not names:
        return "target", [f"source maps it to service {', '.join(sorted(services & cx['terms']))}, which the question names", *support], None
    if dependents & (names | cx["terms"]):
        return "dependency", [f"declared as documentation for a dependency of {', '.join(sorted(dependents & (names | cx['terms'])))}", *support], None
    if services and names:  # written for another service: nothing weaker overrides that
        others = ", ".join(sorted(services))
        if mentioned and relevant:
            return "unverified", [f"source maps it to service {others}", f"mentions {', '.join(mentioned[:3])}", share], None
        return "unrelated", [f"source maps it to service {others}", *support], f"written for {others}, not {', '.join(sorted(names)[:3])}"
    # 2. repository, identifiers and names, with no explicit mapping against them
    basis: list[str] = []
    if repo and cx["repository"] and repo == cx["repository"]:
        basis.append(f"from the target's repository {repo}")
    elif short and short in names:
        basis.append(f"repository {repo} is named like the target")
    ids = sorted(i for i in cx["identifiers"] if i in hay)
    if ids:
        basis.append(f"names {', '.join(ids[:3])}")
    if mentioned:
        basis.append(f"mentions {', '.join(mentioned[:3])}")
    if basis and not services:
        return "target", basis + support, None
    if services and not names and not cx["repository"]:
        if lexical < LEXICAL:
            return "low_relevance", [share], "shares too little with this request to help"
        return "unverified", [f"source maps it to service {', '.join(sorted(services))}", "nothing yet shows that is what the question is about", *support], None
    # 3. relevance by content
    if not relevant:
        return "low_relevance", [share, *support], "shares too little with this request to help"
    why = [share] + (["ranked high by hybrid search"] if mode in ("hybrid", "vector") and lexical < LEXICAL else [])
    if declared_shared:
        return "shared", why + ["the source declares it shared documentation for every service in its scope", *support], None
    if not services and kind_of(path) in ("markdown", "text") and (ns == "*" or support):
        label = "documentation with no service binding: general guidance" if ns == "*" else f"documentation for namespace {ns} with no service binding: not specific to the target"
        return "shared", why + [label, *support], None
    if not names and not cx["repository"]:
        return "shared", why + ["the request names no target, so nothing ties it more closely", *support], None
    return "unverified", why + [f"{repo or 'its source'} is not established as the target's", *support], None


def reclassify(inv: Any, question: str) -> None:
    """Context grows as the run goes: triage names the workloads. Every passage is judged again on what is known now."""
    st = inv.knowledge
    if not st["passages"]:
        return
    cx = context(inv, question)
    hits = {h.get("chunk_id"): h for e in inv.evidence if e["tool"] == "kb_search" and e["ok"] and isinstance(e.get("data"), list) for h in e["data"] if isinstance(h, dict)}
    for p in st["passages"]:
        hit = hits.get(p["chunk_id"])
        if hit is None:
            continue
        mode = (st["queries"][p["query"]] if p["query"] < len(st["queries"]) else {}).get("mode")
        p["authority"], p["basis"], p["excluded"] = classify(hit, cx, p["rank"] - 1, mode)
        if not p.get("cited"):
            p["state"] = "supplied" if p.get("provided") and p["authority"] in USABLE else "eligible" if p["authority"] in USABLE else "retrieved"
    _settle(st)


def passage_id(ref: str, rank: int) -> str:
    """E3.2: the second passage of the search stored as E3. Stable for the life of the investigation, because
    the search result it points into is stored evidence."""
    return f"{ref}.{rank}"


def _passage(hit: dict, ref: str, query_index: int, rank: int, authority: str, basis: list[str], excluded: str | None) -> dict:
    cit, fr = hit.get("citation") or {}, hit.get("freshness") or {}
    return {"id": passage_id(ref, rank + 1), "ref": ref, "chunk_id": hit.get("chunk_id"), "document_id": hit.get("document_id"), "query": query_index, "rank": rank + 1,
            "authority": authority, "basis": basis, "excluded": excluded, "state": "eligible" if authority in USABLE else "retrieved",
            "used": False, "cited": False, "cited_by": None, "provided": False, "source": cit.get("source"), "source_type": cit.get("source_type"), "repository": cit.get("repository"),
            "path": cit.get("path"), "revision": cit.get("revision"), "lines": cit.get("lines"), "url": cit.get("url"), "title": hit.get("title"), "heading": hit.get("heading"),
            "freshness": {k: fr.get(k) for k in ("state", "reason", "trust", "last_checked", "source_synced", "possible_conflict") if fr.get(k) is not None},
            "excerpt": redact(" ".join(str(hit.get("text") or "").split()))[:EXCERPT_CHARS]}


# ---- the stage --------------------------------------------------------------------------------------


def _prior(inv: Any, args: dict) -> dict | None:
    """The same search already stored in this investigation, from before a crash or retry."""
    return next((e for e in inv.evidence if e["tool"] == "kb_search" and e["args"].get("query") == args["query"] and e["args"].get("k") == args["k"]), None)


async def _search(inv: Any, executor: Any, args: dict, purpose: str) -> tuple[dict | None, bool]:
    """Evidence for one search and whether it was reused. A resumed run reads what it stored, not the index again."""
    prior = _prior(inv, args)
    if prior is not None:
        return prior, True
    st = inv.knowledge
    st["calls"] = st.get("calls", 0) + 1  # reserved first: the call comes from the knowledge allowance, not the live budget
    out = await inv._call(executor, "kb_search", args, purpose)
    if out is None:
        st["calls"] -= 1
        return None, False
    return {"ref": out.evidence_ref, "ok": out.ok, "data": out.data, "source": out.source or {}, "error": out.error, "error_kind": out.error_kind, "summary": out.summary}, False


async def run(inv: Any, executor: Any, stage: str, question: str, queries: list[str] | None = None, purpose: str | None = None) -> list[dict]:
    """One bounded stage. Returns the passages it added. Never raises for a retrieval problem: the investigation
    goes on with the evidence it has and the gap is recorded."""
    st = inv.knowledge
    if stage in st["stages"]:
        return []  # finished before a crash; its passages came back with the checkpoint
    started = time.monotonic()
    req = inv.request
    st["scope"] = {"environment": executor.ctx.environment.name, "namespace": executor.ctx.focus_namespace or None,
                   "target": req.target_name if req else None, "repository": req.repository if req else None,
                   "namespaces": list(req.namespaces) if req else [], "history_used": bool(inv.history)}
    if queries is None:
        queries = initial_queries(inv, question) if stage == "initial" else [q for q in [refresh_query(inv)] if q]
    limit = MAX_INITIAL if stage != "refresh" else MAX_REFRESH
    reason = None
    if not queries:
        st["stages"].append(stage)
        return []
    if inv._outside_request("kb_search", {"query": queries[0], "k": K}):
        reason = "out_of_request"
    elif st.get("calls", 0) >= inv.settings.knowledge_max_searches:
        reason = "budget"
    elif inv.time_left() < MIN_SECONDS_LEFT:
        reason = "time"
    if reason:
        for q in queries[:limit]:
            st["queries"].append({"stage": stage, "query": q[:300], "status": "not_run", "reason": REASONS[reason]})
        if reason not in st["reasons"]:
            st["reasons"].append(reason)
        _settle(st)
        st["stages"].append(stage)
        await inv._emit("knowledge", {"stage": stage, "status": st["status"], "reason": REASONS[reason]})
        KNOWLEDGE_RETRIEVALS.labels(stage, "skipped").inc()
        return []
    reclassify(inv, question)
    cx = context(inv, question)
    seen = {p["chunk_id"] for p in st["passages"]}
    added: list[dict] = []
    for q in queries[:limit]:
        args = {"query": q[:500], "k": K}
        if st.get("calls", 0) >= inv.settings.knowledge_max_searches and _prior(inv, args) is None:
            st["queries"].append({"stage": stage, "query": q[:300], "status": "not_run", "reason": REASONS["budget"]})
            continue
        index = len(st["queries"])
        e, reused = await _search(inv, executor, args, purpose or ("knowledge: documented procedures for this request" if stage == "initial" else "knowledge: what the runbooks say about these symptoms"))
        row: dict[str, Any] = {"stage": stage, "query": q[:300], "ref": e["ref"] if e else None, "reused": reused}
        if e is None:
            row.update(status="not_run", reason="already searched, or the tool-call budget is used up")
        elif not e["ok"]:
            row.update(status="failed", reason=(e.get("error_kind") or "error") + ": " + redact(str(e.get("error") or ""))[:160])
            if "failed" not in st["reasons"]:
                st["reasons"].append("failed")
        else:
            src = e.get("source") or {}
            judged = src.get("evidence") or {}
            hits = e["data"] if isinstance(e.get("data"), list) else []
            row.update(status="ok", results=len(hits), evidence=judged.get("state"), mode=src.get("mode"), fallback=src.get("fallback_reason"), latency_ms=src.get("latency_ms"))
            for rank, hit in enumerate(hits):
                if hit.get("chunk_id") in seen:
                    continue
                seen.add(hit.get("chunk_id"))
                authority, basis, excluded = classify(hit, cx, rank, src.get("mode"))
                p = _passage(hit, e["ref"], index, rank, authority, basis, excluded)
                st["passages"].append(p)
                added.append(p)
        st["queries"].append(row)
    _settle(st)
    st["stages"].append(stage)
    elapsed = int((time.monotonic() - started) * 1000)
    usable = sum(1 for p in added if p["authority"] in USABLE)
    await inv._emit("knowledge", {"stage": stage, "status": st["status"], "queries": len(queries[:limit]), "passages": len(added), "usable": usable})
    KNOWLEDGE_RETRIEVALS.labels(stage, st["status"]).inc()
    modes = sorted({q.get("mode") for q in st["queries"] if q.get("mode")})
    log.info("knowledge retrieved", extra={"event": "knowledge_retrieval", "investigation_id": str(inv.id), "operation": stage, "outcome": st["status"], "durationMs": elapsed,
                                           "mode": ",".join(modes), "fallback": any(q.get("fallback") for q in st["queries"]), "passages": len(added), "usable": usable})
    return added


def _settle(st: dict) -> None:
    ran = [q for q in st["queries"] if q["status"] in ("ok", "failed")]
    usable = [p for p in st["passages"] if p["authority"] in USABLE]
    st["reasons"] = [r for r in st["reasons"] if r not in ("none", "not_attempted")]
    if usable:
        st["status"] = "partial" if any(q["status"] == "failed" for q in ran) else "retrieved"
    elif ran and all(q["status"] == "failed" for q in ran):
        st["status"] = "failed"
    elif ran:
        st["status"] = "empty"
        if "none" not in st["reasons"]:
            st["reasons"].append("none")
    else:
        st["status"] = "not_attempted"
        if not st["reasons"]:
            st["reasons"].append("not_attempted")


def for_checkpoint(st: dict) -> dict:
    """Decisions and progress only: passage text stays in the evidence table."""
    return {**st, "passages": [{k: v for k, v in p.items() if k != "excerpt"} for p in st["passages"]]}


def restore(inv: Any, saved: dict | None) -> dict:
    st = {**new_state(), **(saved or {})}
    body = texts(inv)
    for p in st["passages"]:
        p.setdefault("excerpt", redact(" ".join(body.get(p.get("chunk_id"), "").split()))[:EXCERPT_CHARS])
        _upgrade(p)
    return st


def _upgrade(p: dict) -> dict:
    """A passage recorded before passage ids existed (checkpoints and reports from before this version)."""
    if p.get("ref") and p.get("rank") and not p.get("id"):
        p["id"] = passage_id(p["ref"], p["rank"])
    p.setdefault("cited", bool(p.get("used")))
    p.setdefault("state", "cited" if p.get("used") else "supplied" if p.get("provided") else "eligible" if p.get("authority") in USABLE else "retrieved")
    return p


def usable(st: dict) -> list[dict]:
    order = {"target": 0, "dependency": 1, "shared": 2, "unverified": 3}
    return sorted((p for p in st["passages"] if p["authority"] in USABLE), key=lambda p: (order[p["authority"]], p["query"], p["rank"]))


def texts(inv: Any) -> dict[str, str]:
    """Passage text by chunk id, from stored evidence: checkpoints never carry it."""
    out: dict[str, str] = {}
    for e in inv.evidence:
        if e["tool"] == "kb_search" and e["ok"] and isinstance(e.get("data"), list):
            for h in e["data"]:
                if isinstance(h, dict) and h.get("chunk_id"):
                    out[h["chunk_id"]] = f"{h.get('heading') or ''}\n{h.get('text') or ''}".strip()
    return out


LABEL = {"target": "about the target", "dependency": "about a declared dependency of the target, not the target itself", "shared": "general guidance, not specific to the target", "unverified": "relationship to the target not established"}


def prompt_block(inv: Any) -> str:
    """Passages for planning and the report, within a fixed budget. Structure from MOKS; text from documents."""
    from . import prompts

    picked = usable(inv.knowledge)
    if not picked:
        why = "; ".join(REASONS.get(r, r) for r in inv.knowledge["reasons"]) or "none retrieved"
        return f"Documented knowledge: none usable ({why}). Say what remains unverified instead of assuming a documented procedure."
    body = texts(inv)
    parts, used = [], 0
    for p in picked:
        head = (f"[{p['id']}] {LABEL[p['authority']]}; {p.get('repository') or p.get('source') or ''}:{p.get('path')}"
                f"{'@' + str(p['revision'])[:12] if p.get('revision') else ''} lines {p.get('lines')}; freshness {p['freshness'].get('state', 'unknown')}")
        block = head + "\n" + prompts.untrusted(f"document {p.get('path')}", body.get(p["chunk_id"], p["excerpt"]), PROMPT_PASSAGE_CHARS)
        if used + len(block) > PROMPT_TOTAL_CHARS:
            break
        parts.append(block)
        used += len(block)
        p["provided"] = True
        p["state"] = "supplied"
    return ("Documented knowledge (retrieved under the requester's access; it says how things are meant to work, never what is true now; "
            "cite a passage by its own id such as E3.2, never by the search's E3 alone, and keep documented procedures apart from observations):\n" + "\n\n".join(parts))


def supplied(st: dict) -> dict[str, str]:
    """Passage id -> evidence ref, for every passage the model was given."""
    return {p["id"]: p["ref"] for p in st["passages"] if p.get("provided") and p.get("id")}


REF = re.compile(r"\bE\d{1,3}\b")
PASSAGE_REF = re.compile(r"\bE\d{1,3}\.\d{1,2}\b")
ANY_REF = re.compile(r"\bE\d{1,3}(?:\.\d{1,2})?\b")


def _citations(report: Any) -> tuple[set[str], set[str], set[tuple[str, str]], set[str]]:
    """(passage ids, bare evidence refs, (ref, path) pairs from guidance items, ids MOKS itself added).
    A claim's passage counts only while the claim still cites that passage's evidence after validation."""
    ids: set[str] = set()
    bare: set[str] = set()
    for c in [*getattr(report, "observed_facts", []), *getattr(report, "suspected_causes", [])]:
        kept = set(getattr(c, "evidence", [])) | set(getattr(c, "contradicting", []))
        ids |= {x for x in getattr(c, "passages", []) if x.rsplit(".", 1)[0] in kept}
        bare |= kept
    for step in getattr(report, "next_steps", []):
        found = set(ANY_REF.findall(step))
        ids |= {x for x in found if "." in x}
        bare |= {x for x in found if "." not in x}
    answer = getattr(report, "answer", None) or {}
    for x in answer.get("cited") or []:
        (ids if "." in str(x) else bare).add(str(x))
    paths: set[tuple[str, str]] = set()
    for section in ("overview", "prerequisites", "steps", "verification", "rollback"):
        for item in answer.get(section) or []:
            for ref in item.get("sources") or []:
                if "." in str(ref):
                    ids.add(str(ref))
                elif item.get("path"):
                    paths.add((ref, item["path"]))
                else:
                    bare.add(ref)
    return ids, bare, paths, set(getattr(report, "validation", {}).get("moks_cited_passages") or [])


def mark_used(inv: Any, report: Any) -> None:
    """A passage is cited when the final answer names its id, it was eligible, and (for a model's answer) the
    model was given it. A bare search ref names a passage only when exactly one passage of that search was
    available to the answer; otherwise the citation is recorded as ambiguous and no passage is credited."""
    st = inv.knowledge
    ids, bare, paths, by_moks = _citations(report)
    llm = report.generated_by == "llm"
    available = [p for p in st["passages"] if p["authority"] in USABLE and (p.get("provided") or not llm or p.get("id") in by_moks)]
    per_ref: dict[str, list[dict]] = {}
    for p in available:
        per_ref.setdefault(p["ref"], []).append(p)
    known = {p.get("id") for p in st["passages"]}
    rejected = sorted(i for i in ids if i not in {p.get("id") for p in available})
    st["citations"] = {"passages": sorted(ids & known), "rejected": [{"id": i, "reason": "no such passage" if i not in known else "cited, but this passage was not available to the answer"}
                                                                   for i in rejected][:20], "ambiguous": []}
    avail = {id(p) for p in available}
    for p in st["passages"]:
        _upgrade(p)
        ok = id(p) in avail
        by_id = ok and p.get("id") in ids
        by_path = ok and (p["ref"], p.get("path")) in paths and sum(1 for q in per_ref.get(p["ref"], []) if q.get("path") == p.get("path")) == 1
        by_ref = ok and p["ref"] in bare and len(per_ref.get(p["ref"], [])) == 1
        p["cited"] = p["used"] = bool(by_id or by_path or by_ref)
        p["cited_by"] = ("moks" if p.get("id") in by_moks else "model" if llm else "rules") if p["cited"] else None
        p["state"] = "cited" if p["cited"] else "supplied" if p.get("provided") and p["authority"] in USABLE else "eligible" if p["authority"] in USABLE else "retrieved"
        if not p["cited"] and ok and p["ref"] in bare and len(per_ref.get(p["ref"], [])) > 1:
            if p["ref"] not in st["citations"]["ambiguous"]:
                st["citations"]["ambiguous"].append(p["ref"])
            p["excluded"] = p["excluded"] or f"the answer cited its search {p['ref']} without naming a passage, and {len(per_ref[p['ref']])} passages of that search were available"
        elif not p["cited"] and not p["excluded"]:
            p["excluded"] = ("not cited in the answer" if p.get("provided") else "eligible, but not given to the model within the prompt budget" if llm and p["authority"] in USABLE
                             else "retrieved but not used in the answer")


def documented_steps(inv: Any, limit: int = 3) -> list[str]:
    """For a report without a model: where the documented procedure is, labeled, never as a fact."""
    out = []
    for p in usable(inv.knowledge)[:limit]:
        where = f"{p.get('repository') + ':' if p.get('repository') else ''}{p.get('path')}"
        lines = p.get("lines") or []
        at = f" lines {lines[0]}-{lines[1]}" if len(lines) == 2 and lines[0] else ""
        label = {"target": "Documented procedure", "dependency": "Documented procedure for a dependency", "shared": "General runbook guidance", "unverified": "Possibly related documentation (relationship to the target not established)"}[p["authority"]]
        out.append(f"{label}: {p.get('heading') or p.get('title') or where} — {where}{at} ({p.get('id') or p['ref']}). Documented, not observed: check it against the live evidence before following it.")
    return out


def summary(inv: Any) -> dict[str, Any]:
    st = inv.knowledge
    picked = usable(st)
    out = {k: st[k] for k in ("status", "scope", "queries", "hints")}
    out["reasons"] = [REASONS.get(r, r) for r in st["reasons"]]
    out["passages"] = [_upgrade(p) for p in st["passages"][:40]]
    cited = sum(bool(p.get("cited")) for p in st["passages"])
    out["counts"] = {"retrieved": len(st["passages"]), "eligible": len(picked), "supplied": sum(bool(p.get("provided")) for p in st["passages"]), "cited": cited,
                     "usable": len(picked), "used": cited, "excluded": sum(1 for p in st["passages"] if p["authority"] not in USABLE)}
    out["citations"] = st.get("citations") or {"passages": [], "rejected": [], "ambiguous": []}
    out["version"] = 2  # passage ids and states; version 1 reports have only used/provided
    if picked and not cited:
        out["reasons"].append("Passages were retrieved but none was cited in the answer.")
    if out["citations"]["ambiguous"]:
        out["reasons"].append(f"The answer cited {', '.join(out['citations']['ambiguous'])} without naming a passage, so no passage of those searches is credited.")
    out["note"] = "Documented knowledge says how things are meant to work. It does not show the current state of any system."
    return out


async def hints(inv: Any, principal: Any, environment: str, namespace: str | None) -> list[str]:
    """Why the index may hold less than expected, from sources this person can read. Nothing about other sources."""
    from ..db import session_scope
    from ..rag import readiness

    try:
        async with session_scope() as db:
            return await readiness.hints(db, principal, environment, namespace)
    except Exception:  # a diagnostic must never fail the answer
        log.warning("knowledge hints unavailable", extra={"event": "knowledge_hints_failed", "investigation_id": str(inv.id)})
        return []
