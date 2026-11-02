"""Investigation report schema, LLM output validation and the rule-based fallback report."""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from . import evidence as ev

REF = re.compile(r"^E\d{1,3}$")


class Fact(BaseModel):
    statement: str = Field(max_length=1000)
    evidence: list[str] = Field(default_factory=list)
    kind: str = "observation"  # observation | collection | documentation | historical; set by validate_claims, never by the model
    qualifiers: list[str] = Field(default_factory=list)


class Cause(BaseModel):
    cause: str = Field(max_length=1000)
    confidence: Literal["low", "medium", "high"] = "low"
    evidence: list[str] = Field(default_factory=list)
    reasoning: str = Field(default="", max_length=1200)
    verified: bool = False  # set only by verify_causes; never by the model
    contradicting: list[str] = Field(default_factory=list)  # evidence refs against this cause
    origin: str = "model"  # model | rules | demoted_fact, the last being a statement that could not stand as a fact


class Remediation(BaseModel):
    action: str = Field(max_length=600)
    target: str = Field(default="", max_length=300)
    expected_impact: str = Field(default="", max_length=600)
    risk: str = Field(default="", max_length=600)
    rollback: str = Field(default="", max_length=600)
    requires_approval: bool = True
    executable: bool = False  # a report never executes anything; action plans do


class Report(BaseModel):
    summary: str = Field(default="", max_length=2000)
    observed_facts: list[Fact] = Field(default_factory=list)
    suspected_causes: list[Cause] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    proposed_remediation: list[Remediation] = Field(default_factory=list)
    generated_by: Literal["llm", "rules"] = "rules"
    notes: list[str] = Field(default_factory=list)
    validation: dict[str, Any] = Field(default_factory=dict)
    verified_causes: list[dict[str, Any]] = Field(default_factory=list)  # verified configuration defects only
    verified_findings: list[dict[str, Any]] = Field(default_factory=list)  # every check that passed, by category
    withheld_findings: list[dict[str, Any]] = Field(default_factory=list)  # observed, but a skill's required evidence is missing
    conflicts: list[dict[str, Any]] = Field(default_factory=list)


def parse_json_object(text: str) -> dict[str, Any]:
    """Extract the first JSON object from model output (tolerates code fences and prose)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        obj = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object in model output") from None
        obj = json.loads(text[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("model output is not a JSON object")
    return obj


def validate_llm_report(raw: dict[str, Any], valid_refs: set[str], notes: list[str]) -> Report:
    """Structure and the fields a model may not set. Whether its claims stand is validate_claims' job."""
    try:
        rep = Report.model_validate({**raw, "generated_by": "llm", "notes": notes})
    except ValidationError as exc:
        raise ValueError(f"report schema invalid: {exc.errors()[0]['msg']}") from None
    for f in rep.observed_facts:
        f.kind, f.qualifiers = "observation", []
    for c in rep.suspected_causes:
        c.verified, c.origin = False, "model"
    for r in rep.proposed_remediation:
        r.requires_approval = True
        r.executable = False
    rep.verified_causes, rep.verified_findings, rep.conflicts = [], [], []
    rep.validation = {"valid_refs": sorted(valid_refs, key=lambda r: int(r[1:]))}
    return rep


def _keep_refs(refs: list[str], ledger: ev.Ledger, *kinds: str) -> tuple[list[str], list[str]]:
    kept = [r for r in refs if r in ledger.entries and ledger.entries[r].kind in kinds and ledger.in_scope(ledger.entries[r])]
    return kept, [r for r in refs if r not in kept]


def validate_claims(rep: Report, ledger: ev.Ledger, findings: list[dict], *, check_support: bool = True) -> Report:
    """Every claim keeps only the citations that can show it. What cannot stand as a fact is qualified,
    returned as a hypothesis, recorded as missing evidence or removed, and each decision is recorded.
    `check_support` is off for the rule-based report, whose sentences come from the evidence itself."""
    facts: list[Fact] = []
    decisions: dict[str, list[dict]] = {"qualified": [], "hypothesis": [], "missing": [], "dropped": []}
    demoted: list[Cause] = []
    for f in rep.observed_facts:
        v = ev.judge(f.statement, f.evidence, ledger, findings, check_support=check_support)
        if v.disposition in ("accepted", "qualified"):
            f.evidence, f.kind, f.qualifiers = v.refs, v.kind, v.reasons
            facts.append(f)
            if v.disposition == "qualified":
                decisions["qualified"].append({"statement": f.statement[:200], "reasons": v.reasons})
            continue
        decisions[v.disposition].append({"statement": f.statement[:200], "reasons": v.reasons})
        if v.disposition == "hypothesis":
            demoted.append(Cause(cause=f.statement, evidence=v.refs, contradicting=v.contradicting, reasoning=f"Not shown as a fact: {'; '.join(v.reasons)}.", origin="demoted_fact"))
        elif v.disposition == "missing":
            rep.missing_evidence.append(f"Not established: {f.statement} ({'; '.join(v.reasons)}).")
    rep.observed_facts = facts

    downgraded = 0
    for c in rep.suspected_causes:
        c.verified = False
        c.evidence, rejected = _keep_refs(c.evidence, ledger, ev.OBSERVATION, ev.INCOMPLETE, ev.HISTORICAL, ev.DOCUMENTATION)
        c.contradicting = _keep_refs(c.contradicting, ledger, ev.OBSERVATION, ev.INCOMPLETE)[0]
        live = [r for r in c.evidence if ledger.entries[r].kind in ev.LIVE]
        if rejected:
            decisions.setdefault("citations_removed", []).append({"statement": c.cause[:200], "refs": rejected})
        if not live and c.confidence != "low":
            c.confidence = "low"
            downgraded += 1
        if ev.CONFIRMED.search(c.cause) or ev.CONFIRMED.search(c.reasoning):
            c.reasoning = (c.reasoning + " (Not verified: MOKS has not confirmed this cause.)").strip()
            c.confidence = "low" if not live else c.confidence
    rep.suspected_causes = rep.suspected_causes + demoted

    kept, removed = [], []
    for sentence in ev.sentences(rep.summary):
        ok, why = ev.judge_sentence(sentence, ledger, findings) if check_support else (True, None)
        if ok:
            kept.append(sentence)
        else:
            removed.append({"sentence": sentence[:300], "reason": why})
    if removed:
        kept.append(_honest_ending(rep, ledger))
    rep.summary = " ".join(kept) if kept else _honest_ending(rep, ledger)

    rep.conflicts = ev.conflicts(ledger, findings)
    for c in rep.conflicts:
        rep.missing_evidence.append(f"Sources disagree: {c['detail']} ({', '.join(c['evidence'])}).")
    for f in rep.observed_facts:
        against = [c["subject"] for c in rep.conflicts if set(c["evidence"]) & set(f.evidence)]
        if against:
            f.qualifiers.append(f"another source disagrees about {against[0]}")
    rep.validation |= {
        "method": "deterministic: evidence kind, scope, freshness, coverage, findings and whether named identifiers occur in the citation. This is not proof a passage supports a claim.",
        "model_assisted": False, "facts_kept": len(facts), "uncited_facts_dropped": len(decisions["dropped"]), "causes_downgraded": downgraded,
        "decisions": decisions, "summary_sentences_removed": removed, "evidence_kinds": {r: e.kind for r, e in ledger.entries.items()},
    }
    return rep


def _honest_ending(rep: Report, ledger: ev.Ledger) -> str:
    """What may be said when a drafted summary could not stand."""
    failed = ledger.by_kind(*ev.FAILED)
    if rep.observed_facts:
        tail = f" {len(failed)} evidence requests failed." if failed else ""
        return f"The drafted summary went beyond the evidence; see the {len(rep.observed_facts)} facts and the hypotheses below.{tail}"
    if failed:
        return f"MOKS could not establish the state of the system: {len(failed)} evidence requests failed and nothing else supports a conclusion."
    return "MOKS could not establish an answer from the evidence collected."


# Failures a detector reads directly. A crashloop is one of these and its cause is still open.
OBSERVED_FAILURES = {"crash_log_reason", "crashloop", "oomkilled", "image_pull", "unschedulable", "argocd_sync_failed"}
# A wrong setting read from the object itself: a selector that matches no pods IS the broken selector.
CONFIGURATION_DEFECTS = {"service_selector_mismatch", "config_ref_missing", "argocd_source_missing", "argocd_comparison_error"}
ARGO_QUESTION = re.compile(r"(?i)\b(argo|gitops|syncs?\b)")


def focus(question: str, findings: list[dict]) -> list[dict]:
    """A question about Argo CD is answered by Argo CD's findings, even when pods score as more severe."""
    return sorted(findings, key=lambda f: not f["kind"].startswith("argocd_")) if ARGO_QUESTION.search(question) else findings


def verify_causes(report: Report, findings: list[dict], ledger: ev.Ledger | None = None, withheld: dict[str, str] | None = None) -> None:
    """Verified means a named check read the condition from a successful, in-scope observation of a
    specific target. Model confidence never counts: however sure the LLM is, its causes stay suspected.
    Observing a failure does not establish why it happened, so only a configuration defect goes in
    verified_causes. Recovery is validated by action plans after a change, never by a report."""
    for c in report.suspected_causes:
        c.verified = False
    verified, held = [], []
    for f in findings:
        kind = f.get("kind")
        category = "configuration_defect" if kind in CONFIGURATION_DEFECTS else "observed_failure" if kind in OBSERVED_FAILURES else None
        refs = f.get("evidence") or []
        if ledger is not None:
            refs = [r for r in refs if r in ledger.entries and ledger.entries[r].kind in ev.LIVE and ledger.in_scope(ledger.entries[r])]
        if not category or not refs:
            continue
        if kind in (withheld or {}):
            held.append({"kind": kind, "cause": f["title"], "reason": withheld[kind], "evidence": refs})
            continue
        verified.append({"category": category, "check": f"deterministic detector: {kind}", "result": "condition present", "target": (f.get("resources") or [None])[0],
                         "resources": f.get("resources", []), "namespace": f.get("namespace"), "at": ledger.as_of(refs) if ledger else None, "evidence": refs,
                         "cause": f["title"], "detail": f.get("detail", "")})
    report.verified_findings, report.withheld_findings = verified, held
    report.verified_causes = [v for v in verified if v["category"] == "configuration_defect"]


def _scope_text(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in args.items() if k in ("namespace", "pod", "deployment", "promql", "trace_id")) or "cluster"


def rules_report(question: str, findings: list[dict], evidence: list[dict], notes: list[str]) -> Report:
    """Deterministic report from detector findings, used without an LLM or when it fails."""
    facts: list[Fact] = []
    causes: list[Cause] = []
    steps: list[str] = []
    remediation: list[Remediation] = []
    findings = focus(question, findings)
    for f in findings[:12]:
        refs = f.get("evidence", [])
        facts.append(Fact(statement=f"{f['title']}. {f['detail']}", evidence=refs))
        if f.get("suspected_cause"):
            causes.append(Cause(cause=f"{f['title']}: {f['suspected_cause']}",
                                confidence="medium" if f["severity"] == "high" else "low", evidence=refs,
                                reasoning="Rule-based detector match; not verified.", origin="rules"))
        steps.extend(s for s in f.get("next_checks", []) if s not in steps)
        if f.get("remediation_hint"):
            remediation.append(Remediation(action=f["remediation_hint"], target=", ".join(f.get("resources", [])),
                                           expected_impact="Restores the affected resource if the suspected cause is correct.",
                                           risk="Review before applying; validate afterwards.",
                                           rollback="Revert the change through the same reviewed process."))
    # Baseline facts straight from evidence summaries, so the report always states what was checked.
    for e in evidence:
        if e["ok"] and e.get("kind", ev.OBSERVATION) != ev.DOCUMENTATION and len(facts) < 16:
            facts.append(Fact(statement=f"{e['tool']} ({_scope_text(e['args'])}): {e['summary']}", evidence=[e["ref"]]))
    failed = [e for e in evidence if not e["ok"]]
    missing = [f"{e['tool']} failed ({e.get('error')}); its evidence is missing." for e in failed]
    if any(e.get("truncated") for e in evidence):
        missing.append("Some results were cut short, so a resource that is not listed may still exist.")
    if not findings:
        missing.append("No rule-based detector matched. Application-level metrics or logs may be needed to explain the symptom.")
    ok = [e for e in evidence if e["ok"]]
    if findings:
        summary = f"{len(findings)} problem(s) detected from {len(ok)} successful evidence queries."
    elif failed:  # silence from a check that did not run says nothing about what it would have found
        summary = f"No detector matched in {len(ok)} successful evidence queries, but {len(failed)} failed, so this does not show the system is healthy."
    else:
        summary = f"No Kubernetes-level problems detected in {len(ok)} successful evidence queries."
    argo = [f["resources"][0].partition("/")[2] for f in findings if f["kind"].startswith("argocd_")]
    if argo and ARGO_QUESTION.search(question):
        gone = sum(f["kind"] == "argocd_source_missing" for f in findings)
        summary = f"Argo CD reports problems with {len(argo)} applications: {', '.join(argo)}."
        if gone:
            summary += f" {gone} read a path that no longer exists in Git, so Argo CD cannot build their manifests."
    elif findings:
        summary += f" Most severe: {findings[0]['title']}."
    return Report(summary=summary, observed_facts=facts, suspected_causes=causes, missing_evidence=missing,
                  next_steps=steps[:10], proposed_remediation=remediation[:5], generated_by="rules", notes=notes)
