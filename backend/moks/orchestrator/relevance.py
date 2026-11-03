"""The last check before an answer leaves: does it answer what was asked, about what was asked?

Structured and deterministic, so it does not depend on a model judging its own answer. When a check fails,
MOKS makes one bounded correction (removing what is off target or unsupported, stating what is missing) and
checks again. It never loops: an answer that still fails goes out with an explanation of what is missing.
"""
from __future__ import annotations

import re
from typing import Any

from . import evidence as ev
from .intent import GUIDANCE_TYPES, RequestContext

GENERIC = re.compile(r"(?i)^\s*(fix|resolve|address|investigate|look into|check|handle) (what|whatever|the (issue|problem|error|condition|failure))s?\b(?!.*[`/])|fix what the condition names")
INCIDENT_TYPES = {"troubleshooting", "health_check"}


def _check(name: str, passed: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail}


def _off_target(payload: dict, req: RequestContext, ledger: ev.Ledger) -> tuple[list[int], list[str]]:
    """Facts that cite only evidence from outside the target, and findings about another namespace."""
    facts = []
    for i, f in enumerate(payload.get("observed_facts") or []):
        entries = [ledger.entries[r] for r in f.get("evidence") or [] if r in ledger.entries]
        if entries and all(e.namespace not in (None, "*") and not req.in_scope(e.namespace) for e in entries):
            facts.append(i)
    findings = [f.get("title", "") for f in payload.get("findings") or [] if f.get("namespace") and not req.in_scope(f.get("namespace"))]
    return facts, findings


def _guidance_items(payload: dict) -> list[dict]:
    a = payload.get("answer") or {}
    return [x for s in ("prerequisites", "steps", "verification", "rollback") for x in a.get(s) or []]


def run_checks(payload: dict, req: RequestContext, ledger: ev.Ledger, findings: list[dict]) -> list[dict]:
    rtype = payload.get("response_type")
    answer = payload.get("answer") or {}
    checks = [_check("response_matches_intent", rtype == req.response_type, f"answered as {rtype}, asked for {req.response_type}")]

    if rtype in GUIDANCE_TYPES:
        has_steps = bool(answer.get("steps"))
        gaps = answer.get("gaps") or []
        checks.append(_check("answers_question", has_steps or bool(gaps), "steps from the sources" if has_steps else f"{len(gaps)} gap(s) stated" if gaps else "neither steps nor an explanation of what is missing"))
        unsupported = [x for x in _guidance_items(payload) if x.get("command") and (not x.get("sources") or x.get("support") not in ("quoted",))]
        uncited = [x for x in _guidance_items(payload) if not x.get("sources")]
        checks.append(_check("instructions_supported", not unsupported and not uncited, f"{len(unsupported)} command(s) and {len(uncited)} item(s) without a supporting source" if unsupported or uncited else "every item cites a source"))
        incident = [f for f in findings if f.get("severity") in ("high", "medium") and not req.in_scope(f.get("namespace"))]
        checks.append(_check("not_displaced", not incident, f"{len(incident)} finding(s) about other targets" if incident else "no unrelated findings"))
        if rtype == "execution_plan":
            ex = answer.get("execution") or {}
            checks.append(_check("nothing_executed", ex.get("executed") is False, "execution goes through action plans"))
    elif rtype == "conceptual":
        checks.append(_check("answers_question", bool((payload.get("summary") or "").strip()), "a direct answer"))
        live = [e for e in ledger.entries.values() if e.tool != "kb_search"]
        checks.append(_check("no_live_queries", not live, f"{len(live)} live queries" if live else "documentation only"))
    else:
        checks.append(_check("answers_question", bool((payload.get("summary") or "").strip()) and bool(payload.get("observed_facts") or payload.get("missing_evidence")),
                             "a summary with facts or stated gaps"))
        facts, other = _off_target(payload, req, ledger)
        checks.append(_check("on_target", not facts and not other, f"{len(facts)} fact(s) and {len(other)} finding(s) about other targets" if facts or other else "about the requested target"))
        generic = [s for s in payload.get("next_steps") or [] if GENERIC.search(s)] + [r.get("action", "") for r in payload.get("proposed_remediation") or [] if GENERIC.search(r.get("action", ""))]
        checks.append(_check("actionable", not generic, f"{len(generic)} generic step(s)" if generic else "steps name concrete checks"))

    unsupported_summary = [s for s in ev.sentences(payload.get("summary") or "") if ev.REASSURE.search(s) and ev.WORKLOAD_CLAIM.search(s)
                           and not any(e.tool in ev.WORKLOAD_TOOLS and e.kind in ev.LIVE and req.in_scope(e.namespace) for e in ledger.entries.values())]
    checks.append(_check("claims_supported", not unsupported_summary, "workload reassurance without workload evidence" if unsupported_summary else
                         f"{(payload.get('validation') or {}).get('facts_kept', 0)} fact(s) kept after claim validation"))
    stated = " ".join([*(payload.get("missing_evidence") or []), *[g.get("detail", "") for g in answer.get("gaps") or []]]).lower()
    unstated = [u for u in req.unresolved if u != "target" and u not in stated]
    if "target" in req.unresolved and "name" not in stated and "what to deploy" not in stated:
        unstated.append("target")
    checks.append(_check("uncertainty_stated", not unstated, f"not stated: {', '.join(unstated)}" if unstated else "unknowns are stated"))
    return checks


def correct(payload: dict, req: RequestContext, ledger: ev.Ledger, failed: set[str]) -> list[str]:
    """One pass of fixes for what failed. Returns what was done."""
    done: list[str] = []
    if "response_matches_intent" in failed:
        payload["response_type"] = req.response_type
        done.append(f"response type set to {req.response_type}")
    if "on_target" in failed or "not_displaced" in failed:
        facts, _ = _off_target(payload, req, ledger)
        if facts:
            payload["observed_facts"] = [f for i, f in enumerate(payload["observed_facts"]) if i not in facts]
            done.append(f"removed {len(facts)} fact(s) about other targets")
        moved = [f for f in payload.get("findings") or [] if f.get("namespace") and not req.in_scope(f.get("namespace"))]
        if moved:
            payload["findings"] = [f for f in payload["findings"] if f not in moved]
            payload.setdefault("excluded", []).append({"reason": "about another target", "count": len(moved), "namespaces": sorted({f["namespace"] for f in moved}), "evidence": sorted({r for f in moved for r in f.get("evidence", [])})})
            done.append(f"moved {len(moved)} finding(s) about other targets out of the answer")
    if "instructions_supported" in failed:
        for section in ("prerequisites", "steps", "verification", "rollback"):
            items = (payload.get("answer") or {}).get(section) or []
            kept = [x for x in items if x.get("sources")]
            for x in kept:
                if x.get("command") and x.get("support") != "quoted":
                    x["command"] = None
            payload["answer"][section] = kept
        done.append("removed instructions no source supports")
    if "actionable" in failed:
        payload["next_steps"] = [s for s in payload.get("next_steps") or [] if not GENERIC.search(s)]
        payload["proposed_remediation"] = [r for r in payload.get("proposed_remediation") or [] if not GENERIC.search(r.get("action", ""))]
        done.append("removed generic steps")
    if "claims_supported" in failed:
        kept = [s for s in ev.sentences(payload.get("summary") or "") if not (ev.REASSURE.search(s) and ev.WORKLOAD_CLAIM.search(s))]
        payload["summary"] = " ".join(kept + ["The state of the running workloads was not checked."])
        done.append("removed workload reassurance the evidence does not support")
    if "uncertainty_stated" in failed:
        for u in req.unresolved:
            cands = req.candidates.get(u)
            payload.setdefault("missing_evidence", []).append(f"Not established: the {u} for this request" + (f" (candidates: {', '.join(cands)})" if cands else "") + ".")
        done.append("stated what could not be resolved")
    if "answers_question" in failed and payload.get("response_type") in GUIDANCE_TYPES:
        payload["answer"].setdefault("gaps", []).append({"kind": "insufficient_evidence", "detail": "MOKS found no steps for this in the sources it could read.", "evidence": []})
        done.append("stated that no steps were found")
    return done


def check(payload: dict, req: RequestContext, ledger: ev.Ledger, findings: list[dict]) -> dict[str, Any]:
    """Validate, correct once, validate again. The result goes into the report and the trace."""
    first = run_checks(payload, req, ledger, findings)
    failed = {c["name"] for c in first if not c["passed"]}
    if not failed:
        return {"status": "passed", "checks": first, "corrections": [], "attempts": 1}
    corrections = correct(payload, req, ledger, failed)
    second = run_checks(payload, req, ledger, [f for f in findings if req.in_scope(f.get("namespace"))] if "not_displaced" in failed else findings)
    still = [c for c in second if not c["passed"]]
    status = "corrected" if not still else "failed"
    if still:
        payload["summary"] = ("This answer did not pass MOKS's relevance checks (" + "; ".join(f"{c['name']}: {c['detail']}" for c in still) + "). " + (payload.get("summary") or ""))[:2000]
    return {"status": status, "checks": second, "first_pass": first, "corrections": corrections, "attempts": 2}
