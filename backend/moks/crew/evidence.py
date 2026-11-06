"""The CrewAI flow's evidence registry and the code that holds the crew's answer to it.

Every tool call the crew makes goes through Investigator._call, so it is stored as an Evidence row (the
same E-numbered refs the rest of MOKS cites). The registry is the crew's view of those rows:
`{id, source, query, timestamp, excerpt, truncated}`, where the excerpt is cut from the stored, redacted
result, never from anything a model wrote. The model sees excerpts, cites ids, and validate() decides in
code what its answer may keep.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..orchestrator import evidence as ev
from ..orchestrator.report import Cause, Fact, Report
from ..redaction import redact

REF = re.compile(r"\bE\d{1,4}\b")
CONFIDENCE = ("low", "medium", "high")


def excerpt_of(data: Any, summary: str, limit: int) -> tuple[str, bool]:
    """A bounded excerpt of a stored result. True when it had to be cut."""
    body = data if isinstance(data, str) else json.dumps(data, default=str, separators=(",", ":"))
    text = redact(f"{summary}\n{body}" if body not in ("null", "") else summary)
    return (text, False) if len(text) <= limit else (text[:limit] + "…[cut]", True)


@dataclass
class Registry:
    excerpt_chars: int = 2000
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    gaps: list[dict[str, str]] = field(default_factory=list)

    def add(self, record: dict[str, Any], *, agent: str, focus: Any = None) -> dict[str, Any]:
        """`record` is what Investigator._call keeps in inv.evidence. `focus` narrows the excerpt to the
        target workload's part of a namespace-wide result; the stored row keeps everything."""
        text, cut = excerpt_of(focus if focus is not None else record.get("data") if record.get("ok") else None,
                               str(record.get("summary") or record.get("error") or ""), self.excerpt_chars)
        entry = {"id": record["ref"], "source": record["tool"], "query": record.get("args") or {}, "timestamp": record.get("collected_at"),
                 "excerpt": text, "truncated": bool(record.get("truncated")) or cut, "ok": bool(record.get("ok")), "kind": record.get("kind"), "agent": agent}
        self.records[entry["id"]] = entry
        if not entry["ok"]:
            self.gap(record["tool"], str(record.get("error") or record.get("summary") or "failed"))
        return entry

    def gap(self, source: str, reason: str) -> None:
        item = {"source": source, "reason": redact(reason)[:300]}
        if item not in self.gaps:
            self.gaps.append(item)

    def listing(self) -> str:
        """One line per record, for the reviewer: what exists to cite."""
        return "\n".join(f"{r['id']}: {r['source']} {json.dumps(r['query'], separators=(',', ':'))} ok={r['ok']} truncated={r['truncated']}"
                         for r in self.records.values()) or "(no evidence was collected)"


def _refs(value: Any) -> list[str]:
    items = value if isinstance(value, list) else [value] if value else []
    out: list[str] = []
    for item in items:
        for ref in REF.findall(str(item)):
            if ref not in out:
                out.append(ref)
    return out


def _text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _cap_confidence(asked: str, refs: list[str], registry: Registry) -> tuple[str, str | None]:
    """Confidence follows the evidence, not the model: none live means low, one live source at most medium,
    high needs live evidence from two different sources."""
    asked = asked if asked in CONFIDENCE else "low"
    live = [registry.records[r] for r in refs if registry.records[r]["ok"] and registry.records[r]["kind"] in ev.LIVE]
    ceiling = "low" if not live else "medium" if len({r["source"] for r in live}) < 2 else "high"
    if CONFIDENCE.index(asked) > CONFIDENCE.index(ceiling):
        return ceiling, f"confidence lowered from {asked} to {ceiling}: " + ("no live evidence cited" if not live else "high needs live evidence from two sources")
    return asked, None


def validate(raw: dict[str, Any], registry: Registry, notes: list[str]) -> tuple[Report, dict[str, Any]]:
    """The crew's answer, held to the registry. A citation of an id the registry does not hold is
    rejected; a finding or cause left with no valid citation is removed and listed; confidence is capped
    by the evidence; any text the model offered as an excerpt or quote is ignored. Returns the report and
    what was decided, for `report.crewai.validation`."""
    rejected: list[str] = []
    removed: list[dict[str, str]] = []
    lowered: list[dict[str, str]] = []

    def keep(refs_in: Any) -> list[str]:
        refs = _refs(refs_in)
        rejected.extend(r for r in refs if r not in registry.records and r not in rejected)
        return [r for r in refs if r in registry.records]

    facts: list[Fact] = []
    for f in raw.get("findings") if isinstance(raw.get("findings"), list) else []:
        if not isinstance(f, dict):
            continue
        statement = _text(f.get("statement"), 1000)
        refs = keep(f.get("evidence"))
        if not statement:
            continue
        if not refs:
            removed.append({"statement": statement[:200], "reason": "finding cites no recorded evidence"})
            continue
        facts.append(Fact(statement=statement, evidence=refs))

    causes: list[Cause] = []
    for c in raw.get("likely_causes") if isinstance(raw.get("likely_causes"), list) else []:
        if not isinstance(c, dict):
            continue
        text = _text(c.get("cause"), 1000)
        refs = keep(c.get("evidence"))
        if not text:
            continue
        if not refs:
            removed.append({"statement": text[:200], "reason": "likely cause cites no recorded evidence"})
            continue
        confidence, why = _cap_confidence(str(c.get("confidence") or "low").lower(), refs, registry)
        if why:
            lowered.append({"statement": text[:200], "reason": why})
        causes.append(Cause(cause=text, confidence=confidence, evidence=refs, reasoning=_text(c.get("reasoning"), 1200), origin="model"))

    missing = [_text(m, 400) for m in raw.get("missing_evidence") or [] if _text(m, 400)][:10] if isinstance(raw.get("missing_evidence"), list) else []
    missing += [f"{g['source']}: {g['reason']}" for g in registry.gaps]
    steps = [_text(s, 400) for s in raw.get("next_steps") or [] if _text(s, 400)][:8] if isinstance(raw.get("next_steps"), list) else []
    summary = _text(raw.get("summary"), 2000)
    if removed or rejected:
        notes.append(f"CrewAI answer validated: {len(rejected)} invented citation(s) rejected, {len(removed)} uncited claim(s) removed.")
    report = Report(summary=summary, observed_facts=facts, suspected_causes=causes, missing_evidence=list(dict.fromkeys(missing)), next_steps=steps,
                    generated_by="llm", notes=list(notes))
    report.validation = {"valid_refs": sorted(registry.records, key=lambda r: int(r[1:]))}
    decided = {"citations_rejected": rejected, "claims_removed": removed, "confidence_lowered": lowered}
    return report, decided


def cited(report: Report) -> list[str]:
    refs: list[str] = []
    for item in [*report.observed_facts, *report.suspected_causes]:
        refs += [r for r in item.evidence if r not in refs]
    return refs
