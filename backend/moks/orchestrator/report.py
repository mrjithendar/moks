"""Investigation report schema, LLM output validation and the rule-based fallback report."""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

REF = re.compile(r"^E\d{1,3}$")


class Fact(BaseModel):
    statement: str = Field(max_length=1000)
    evidence: list[str] = Field(default_factory=list)


class Cause(BaseModel):
    cause: str = Field(max_length=1000)
    confidence: Literal["low", "medium", "high"] = "low"
    evidence: list[str] = Field(default_factory=list)
    reasoning: str = Field(default="", max_length=1200)
    verified: bool = False  # set only by verify_causes; never by the model
    contradicting: list[str] = Field(default_factory=list)  # evidence refs against this cause


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
    verified_causes: list[dict[str, Any]] = Field(default_factory=list)


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


_CONFIRMED = re.compile(r"(?i)\b(root cause (is|was) confirmed|confirmed root cause|has been (resolved|fixed)|is now (resolved|fixed|healthy))\b")


def validate_llm_report(raw: dict[str, Any], valid_refs: set[str], notes: list[str]) -> Report:
    """Validate structure and citations. Facts must cite real evidence; uncited facts are dropped.
    Causes keep only real citations and drop to low confidence without any."""
    try:
        rep = Report.model_validate({**raw, "generated_by": "llm", "notes": notes})
    except ValidationError as exc:
        raise ValueError(f"report schema invalid: {exc.errors()[0]['msg']}") from None
    dropped = 0
    facts = []
    for f in rep.observed_facts:
        f.evidence = [r for r in f.evidence if REF.match(r) and r in valid_refs]
        if f.evidence:
            facts.append(f)
        else:
            dropped += 1
    rep.observed_facts = facts
    downgraded = 0
    for c in rep.suspected_causes:
        c.verified = False
        c.evidence = [r for r in c.evidence if REF.match(r) and r in valid_refs]
        c.contradicting = [r for r in c.contradicting if REF.match(r) and r in valid_refs]
        if not c.evidence and c.confidence != "low":
            c.confidence = "low"
            downgraded += 1
        if _CONFIRMED.search(c.cause) or _CONFIRMED.search(c.reasoning):
            c.reasoning = (c.reasoning + " (Not verified: MOKS has not confirmed this cause.)").strip()
    for r in rep.proposed_remediation:
        r.requires_approval = True
        r.executable = False
    if _CONFIRMED.search(rep.summary):
        rep.summary += " (Unverified: no change has been made or validated.)"
    rep.validation = {"uncited_facts_dropped": dropped, "causes_downgraded": downgraded, "valid_refs": sorted(valid_refs, key=lambda r: int(r[1:]))}
    return rep


# Findings where the detector reads the fault itself, not a symptom of it. A selector that matches
# no pods IS the broken selector; a crashloop is a symptom whose cause is still open.
SELF_EVIDENT = {"crash_log_reason", "service_selector_mismatch", "oomkilled", "image_pull", "unschedulable", "config_ref_missing"}


def verify_causes(report: Report, findings: list[dict]) -> None:
    """Verified means a deterministic check observed the fault directly. Model confidence never
    counts: however sure the LLM is, its causes stay suspected."""
    for c in report.suspected_causes:
        c.verified = False
    report.verified_causes = [{"cause": f["title"], "detail": f.get("detail", ""), "evidence": f.get("evidence", []),
                               "check": f"deterministic detector: {f['kind']}", "resources": f.get("resources", [])}
                              for f in findings if f.get("kind") in SELF_EVIDENT and f.get("evidence")]


def _scope_text(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in args.items() if k in ("namespace", "pod", "deployment", "promql", "trace_id")) or "cluster"


def rules_report(question: str, findings: list[dict], evidence: list[dict], notes: list[str]) -> Report:
    """Deterministic report from detector findings, used without an LLM or when it fails."""
    facts: list[Fact] = []
    causes: list[Cause] = []
    steps: list[str] = []
    remediation: list[Remediation] = []
    for f in findings[:12]:
        refs = f.get("evidence", [])
        facts.append(Fact(statement=f"{f['title']}. {f['detail']}", evidence=refs))
        if f.get("suspected_cause"):
            causes.append(Cause(cause=f"{f['title']}: {f['suspected_cause']}",
                                confidence="medium" if f["severity"] == "high" else "low", evidence=refs,
                                reasoning="Rule-based detector match; not verified."))
        steps.extend(s for s in f.get("next_checks", []) if s not in steps)
        if f.get("remediation_hint"):
            remediation.append(Remediation(action=f["remediation_hint"], target=", ".join(f.get("resources", [])),
                                           expected_impact="Restores the affected resource if the suspected cause is correct.",
                                           risk="Review before applying; validate afterwards.",
                                           rollback="Revert the change through the same reviewed process."))
    # Baseline facts straight from evidence summaries, so the report always states what was checked.
    for e in evidence:
        if e["ok"] and len(facts) < 16:
            facts.append(Fact(statement=f"{e['tool']} ({_scope_text(e['args'])}): {e['summary']}", evidence=[e["ref"]]))
    failed = [e for e in evidence if not e["ok"]]
    missing = [f"{e['tool']} failed ({e.get('error')}); its evidence is missing." for e in failed]
    if not findings:
        missing.append("No rule-based detector matched. Application-level metrics or logs may be needed to explain the symptom.")
    ok = [e for e in evidence if e["ok"]]
    summary = (f"{len(findings)} problem(s) detected from {len(ok)} successful evidence queries."
               if findings else f"No Kubernetes-level problems detected in {len(ok)} successful evidence queries.")
    if findings:
        summary += f" Most severe: {findings[0]['title']}."
    return Report(summary=summary, observed_facts=facts, suspected_causes=causes, missing_evidence=missing,
                  next_steps=steps[:10], proposed_remediation=remediation[:5], generated_by="rules", notes=notes)
