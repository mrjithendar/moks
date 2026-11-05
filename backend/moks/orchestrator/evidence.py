"""Typed evidence and deterministic claim validation.

A citation that exists proves only that a tool ran. Whether it can back a statement depends on what kind
of evidence it is and on the statement: a failed Prometheus query shows that Prometheus could not be
read, and says nothing about whether workloads are healthy. Everything here is rule-based, so the result
is repeatable and a model cannot talk its way past it. These checks cannot prove that a passage means
what a sentence claims. `support` only records that the identifiers a sentence names occur in what it
cites, and the report says so (`validation.method`).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

OBSERVATION = "observation"      # a live read that succeeded and was complete
INCOMPLETE = "incomplete"        # succeeded, but the result was cut short or partial
TOOL_ERROR = "tool_error"
ACCESS_DENIED = "access_denied"
DOCUMENTATION = "documentation"  # what someone wrote down, not the state of the system
HISTORICAL = "historical"        # true when collected; collected before this run resumed or in an earlier one

DOCUMENTATION_TOOLS = {"kb_search", "git_read_file", "similar_incidents"}  # a past incident is a record, not the state of anything now
LIVE = {OBSERVATION, INCOMPLETE}
FAILED = {TOOL_ERROR, ACCESS_DENIED}

DEFAULT_MAX_AGE_SECONDS = 900

COLLECTION = re.compile(r"(?i)\b(quer(y|ies)|requests?|calls?|tools?|connectors?|collection|lookup|fetch\w*|prometheus|loki|grafana|cloudwatch|api|logs?|metrics?)\b.{0,60}\b("
                        r"failed|fails|failing|could not|couldn'?t|cannot|can'?t|unable|unavailable|unreachable|timed? ?out|denied|forbidden|refused|errored|error|not (be )?(collected|available|reachable|read)|missing|incomplete|truncated|partial)\b"
                        r"|\b(could not|unable to|failed to|cannot) (read|reach|query|collect|retrieve|fetch|access|list)\b")
HEALTH = re.compile(r"(?i)\b(healthy|health is good|no (\w+ )?(errors?|issues?|problems?|failures?|alerts?|restarts?|incidents?|anomal\w+)|all (\w+ )?(pods?|workloads?|services?|replicas?|deployments?|containers?|nodes?|checks?)\b|"
                    r"(running|operating|working|performing) (normally|fine|correctly|as expected|well)|nothing (is )?(wrong|failing)|not (failing|crashing|degraded)|resolved|recovered|back to normal|stable)\b")
# Reassurance about running workloads. Only a successful read of those workloads can back it, never a
# report about something else (an Argo CD condition, an alert list) that happens to be live.
REASSURE = re.compile(r"(?i)\b(unaffected|not (been |be )?(affected|impacted)|(keep|keeps|continue|continues) (to )?(run|running|serving|serve)|still (running|serving|up|available)|no (user|customer)[- ]facing impact)\b")
WORKLOAD_CLAIM = re.compile(r"(?i)\b(workloads?|pods?|deployments?|replicas?|containers?|services?|application|app|site|website|traffic|users?)\b")
WORKLOAD_TOOLS = {"k8s_triage", "k8s_list_pods", "k8s_workloads", "k8s_describe_pod", "k8s_services"}
DOCUMENTED = re.compile(r"(?i)\b(runbooks?|documentation|documented|docs?|postmortems?|according to|playbooks?|wiki)\b")
CONFIRMED = re.compile(r"(?i)\b(root cause (is|was|has been) (confirmed|identified|established)|confirmed root cause|has been (resolved|fixed|remediated)|is now (resolved|fixed|healthy))\b")
IDENTIFIER = re.compile(r"(?<![\w./-])[A-Za-z0-9_]+(?:[-/.:][A-Za-z0-9_]+)+(?![\w-])")
NOT_IDENTIFIER = re.compile(r"^(\d+([.:/-]\d+)*|\d{4}-\d{2}-\d{2}.*|[A-Za-z]+/[A-Za-z]+|e\.g|i\.e|read-only|re-run|follow-up|rule-based|out-of-memory|crash-loop|well-known|up-to-date|in-cluster|non-\w+|[a-z]+-[a-z]+)$", re.I)
SENTENCE = re.compile(r"(?<!\be\.g\.)(?<!\bi\.e\.)(?<=[.!?])\s+(?=\S)")


def classify(*, ok: bool, tool: str, denied: bool = False, truncated: bool = False, partial: bool = False) -> str:
    if denied:
        return ACCESS_DENIED
    if not ok:
        return TOOL_ERROR
    if tool in DOCUMENTATION_TOOLS:
        return DOCUMENTATION
    return INCOMPLETE if truncated or partial else OBSERVATION


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _identifiers(text: str) -> list[str]:
    return [m for m in dict.fromkeys(IDENTIFIER.findall(text)) if not NOT_IDENTIFIER.match(m)]


def _in_text(identifier: str, haystack: str) -> bool:
    low = identifier.lower()
    return low in haystack or low.rsplit("/", 1)[-1] in haystack  # the report may say pod/x where evidence says x


@dataclass
class Entry:
    ref: str
    kind: str
    tool: str
    namespace: str | None
    at: datetime | None
    text: str
    summary: str
    data: Any = None


@dataclass
class Ledger:
    """The evidence of one run, indexed for checking claims against it."""
    entries: dict[str, Entry]
    namespace: str = "*"
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS

    @classmethod
    def build(cls, evidence: list[dict], *, namespace: str = "*", now: datetime | None = None, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS) -> "Ledger":
        entries = {}
        for e in evidence:
            if not e.get("ref"):
                continue
            kind = e.get("kind") or classify(ok=e["ok"], tool=e["tool"], denied=bool(e.get("denied")), truncated=bool(e.get("truncated")))
            body = json.dumps({"s": e.get("summary"), "a": e.get("args"), "d": e.get("data")}, default=str).lower()
            entries[e["ref"]] = Entry(e["ref"], kind, e["tool"], (e.get("args") or {}).get("namespace"), _when(e.get("collected_at")), body, str(e.get("summary") or ""), e.get("data"))
        return cls(entries, namespace, now or datetime.now(timezone.utc), max_age_seconds)

    def by_kind(self, *kinds: str) -> list[Entry]:
        return [e for e in self.entries.values() if e.kind in kinds]

    def stale(self, e: Entry) -> bool:
        return e.at is not None and (self.now - e.at).total_seconds() > self.max_age_seconds

    def in_scope(self, e: Entry) -> bool:
        return self.namespace == "*" or e.namespace in (None, "*", self.namespace)

    def live_text(self) -> str:
        return " ".join(e.text for e in self.entries.values() if e.kind not in FAILED)

    def as_of(self, refs: list[str]) -> str | None:
        times = [self.entries[r].at for r in refs if r in self.entries and self.entries[r].at]
        return min(times).strftime("%Y-%m-%d %H:%M UTC") if times else None


SVC_NAMESPACE = re.compile(r"\.([a-z0-9-]+)\.svc\b")


def _alert_namespace(labels: dict) -> str | None:
    """The namespace an alert instance is about: its namespace label, else one named in a service DNS
    address (instance="cart.roboshop-ai.svc:4107"). None when the alert does not say."""
    if labels.get("namespace"):
        return str(labels["namespace"])
    for key in ("instance", "service", "job"):
        m = SVC_NAMESPACE.search(str(labels.get(key) or ""))
        if m:
            return m.group(1)
    return None


def _firing_in_scope(ledger: Ledger, data: Any) -> int:
    """Firing alert instances that could concern the namespace under investigation. Alerts that name
    another namespace say nothing about this one, so they cannot disagree with its Kubernetes checks."""
    count = 0
    for rule in data if isinstance(data, list) else []:
        if not isinstance(rule, dict):
            continue
        for inst in rule["active"] if "active" in rule else [rule]:
            if inst.get("state", "firing") != "firing":
                continue
            ns = _alert_namespace(inst.get("labels") or {})
            if ledger.namespace == "*" or ns is None or ns == ledger.namespace:
                count += 1
    return count


SVC_TARGET = re.compile(r"^([a-z0-9-]+)\.([a-z0-9-]+)\.svc\b")


def _absent_targets(data: Any, triage: list[Entry]) -> tuple[list[str], int]:
    """(alerted services that have no Service in a triaged namespace, firing instances checked). Only for
    namespaces whose triage listed its Services, so a namespace that was not read proves nothing."""
    listed = {t.namespace: {str(s.get("name")) for s in t.data.get("services") or []} for t in triage if isinstance(t.data, dict) and "services" in t.data}
    absent, checked = [], 0
    for rule in data if isinstance(data, list) else []:
        for inst in (rule.get("active") if isinstance(rule, dict) and "active" in rule else [rule]) if isinstance(rule, dict) else []:
            if inst.get("state", "firing") != "firing":
                continue
            m = SVC_TARGET.match(str((inst.get("labels") or {}).get("instance") or ""))
            if m and m.group(2) in listed:
                checked += 1
                if m.group(1) not in listed[m.group(2)]:
                    absent.append(f"{m.group(1)}.{m.group(2)}")
    return sorted(set(absent)), checked


def conflicts(ledger: Ledger, findings: list[dict]) -> list[dict]:
    """Sources that disagree. Marked for the engineer: MOKS does not decide which one is right."""
    out = []
    faults = [f for f in findings if f.get("severity") in ("high", "medium") and f.get("kind") != "recent_rollout"]
    for e in ledger.by_kind(OBSERVATION, INCOMPLETE):
        if e.tool != "prom_alerts" or not _firing_in_scope(ledger, e.data):
            continue
        triage = [t for t in ledger.by_kind(OBSERVATION) if t.tool == "k8s_triage" and ledger.in_scope(t)]
        if triage and not faults:
            absent, checked = _absent_targets(e.data, triage)
            if absent and len(absent) == checked:
                # Both are right: the targets are down because nothing by those names runs. Usually a scrape job or rule outlived its workloads.
                where = sorted({a.split(".", 1)[1] for a in absent})
                detail = (f"Prometheus alerts on {len(absent)} target(s) ({', '.join(a.split('.')[0] for a in absent[:6])}{', …' if len(absent) > 6 else ''}) "
                          f"that have no Service in {', '.join(where)}, so the Kubernetes checks have nothing of theirs to find failing. "
                          "The scrape targets or alert rules are likely stale, or the workloads were removed.")
                out.append({"subject": "alerts on absent services", "evidence": [e.ref, *[t.ref for t in triage[:2]]], "detail": detail, "absent": absent[:20]})
                continue
            out.append({"subject": "firing alerts", "evidence": [e.ref, *[t.ref for t in triage[:2]]],
                        "detail": "Prometheus has firing alerts, but the Kubernetes checks found nothing wrong."})
    seen: dict[tuple, Entry] = {}
    for e in ledger.by_kind(OBSERVATION):
        key = (e.tool, e.text.split('"d":')[0].split('"a":')[-1])
        first = seen.setdefault(key, e)
        if first is not e and first.text != e.text and first.summary != e.summary:
            out.append({"subject": e.tool, "evidence": [first.ref, e.ref], "detail": f"The same {e.tool} query returned different results: {first.summary[:120]} / {e.summary[:120]}"})
    return out


def _contradiction(statement: str, findings: list[dict]) -> list[str]:
    """Evidence of a live fault that a claim of health would have to explain away."""
    named = [i.lower().rsplit("/", 1)[-1] for i in _identifiers(statement)]
    refs: list[str] = []
    for f in findings:
        if f.get("severity") not in ("high", "medium") or not f.get("evidence") or f.get("kind") == "recent_rollout":
            continue
        where = " ".join([str(f.get("namespace") or ""), *[str(r) for r in f.get("resources") or []]]).lower()
        if not named or any(n in where for n in named):
            refs += [r for r in f["evidence"] if r not in refs]
    return refs


@dataclass
class Verdict:
    """How one statement fared."""
    disposition: str          # accepted | qualified | hypothesis | missing | dropped
    kind: str = OBSERVATION   # what the statement is: observation | collection | documentation | historical
    refs: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    contradicting: list[str] = field(default_factory=list)
    stale: bool = False


def judge(statement: str, refs: list[str], ledger: Ledger, findings: list[dict], *, check_support: bool = True) -> Verdict:
    """Decides what a cited statement may be presented as. Order matters: a claim keeps only citations
    of a kind that can show it, and everything after that looks only at those."""
    known = [r for r in refs if r in ledger.entries]
    fabricated = [r for r in refs if r not in ledger.entries]
    reasons = [f"cited {r}, which does not exist" for r in fabricated]
    is_collection = bool(COLLECTION.search(statement))
    is_health = (bool(HEALTH.search(statement)) or bool(REASSURE.search(statement))) and not is_collection
    if not known:
        return Verdict("dropped", reasons=reasons or ["cites no evidence"])

    if is_collection:
        return Verdict("accepted", "collection", known, reasons)

    entries = [ledger.entries[r] for r in known]
    outside = [e.ref for e in entries if not ledger.in_scope(e)]
    if outside:
        reasons.append(f"{', '.join(outside)} came from outside the investigated scope")
        entries = [e for e in entries if e.ref not in outside]
    failed = [e.ref for e in entries if e.kind in FAILED]
    if failed:
        reasons.append(f"{', '.join(failed)} failed, which shows a collection problem and nothing about the system")
    live = [e for e in entries if e.kind in LIVE]
    if not live:
        documents = [e for e in entries if e.kind == DOCUMENTATION]
        history = [e for e in entries if e.kind == HISTORICAL]
        if documents and DOCUMENTED.search(statement) and not is_health:
            return Verdict("accepted", "documentation", [e.ref for e in documents], reasons)
        if documents:
            reasons.append("documentation describes intent, not the current state")
            return Verdict("hypothesis", "documentation", [e.ref for e in documents], reasons)
        if history and not is_health:
            return Verdict("qualified", "historical", [e.ref for e in history], reasons + ["observed earlier and not collected again"])
        if history:
            return Verdict("hypothesis", "historical", [e.ref for e in history], reasons + ["an earlier observation cannot show health now"])
        if failed:
            return Verdict("missing", refs=failed, reasons=reasons)
        return Verdict("hypothesis", refs=known, reasons=reasons)

    used = [e.ref for e in live]
    if check_support:
        names = _identifiers(statement)
        body = " ".join(e.text for e in live)
        absent = [n for n in names if not _in_text(n, body)]
        if absent:
            reasons.append(f"{', '.join(absent[:3])} does not appear in the cited evidence")
            return Verdict("hypothesis", refs=used, reasons=reasons)

    stale = all(ledger.stale(e) for e in live)
    verdict = "qualified" if reasons else "accepted"
    if stale:
        reasons.append(f"collected {ledger.as_of(used)}, older than the {ledger.max_age_seconds // 60}-minute freshness limit")
        verdict = "qualified"
    if is_health:
        if REASSURE.search(statement) and WORKLOAD_CLAIM.search(statement) and not any(e.tool in WORKLOAD_TOOLS for e in live):
            return Verdict("hypothesis", refs=used, reasons=reasons + ["claims workloads are unaffected, but no cited evidence reads those workloads"])
        against = _contradiction(statement, findings)
        if against:
            return Verdict("hypothesis", refs=used, reasons=reasons + [f"contradicted by {', '.join(against)}"], contradicting=against)
        if stale:
            return Verdict("hypothesis", refs=used, reasons=reasons, stale=True)
        cut = [e.ref for e in live if e.kind == INCOMPLETE]
        if cut:
            return Verdict("hypothesis", refs=used, reasons=reasons + [f"{', '.join(cut)} was cut short, so it cannot show the absence of a problem"])
        blind = [e.ref for e in ledger.by_kind(*FAILED)]
        if blind:
            reasons.append(f"{', '.join(blind[:4])} could not be collected, so this covers only part of the system")
            verdict = "qualified"
    return Verdict(verdict, OBSERVATION, used, reasons, stale=stale)


def judge_sentence(sentence: str, ledger: Ledger, findings: list[dict]) -> tuple[bool, str | None]:
    """Summaries carry no citations, so a sentence is held to everything collected. True means keep it."""
    if COLLECTION.search(sentence):
        return True, None
    if CONFIRMED.search(sentence):
        return False, "claims a confirmed cause or a recovery nothing verified"
    absent = [n for n in _identifiers(sentence) if not _in_text(n, ledger.live_text())]
    if absent:
        return False, f"{', '.join(absent[:3])} does not appear in any evidence"
    if not HEALTH.search(sentence) and not REASSURE.search(sentence):
        return True, None
    live = [e for e in ledger.by_kind(OBSERVATION) if not ledger.stale(e)]
    if not live:
        return False, "claims health with no complete, current observation"
    if REASSURE.search(sentence) and WORKLOAD_CLAIM.search(sentence) and not any(e.tool in WORKLOAD_TOOLS and ledger.in_scope(e) for e in live):
        return False, "claims workloads are unaffected, but no evidence read those workloads"
    against = _contradiction(sentence, findings)
    if against:
        return False, f"contradicted by {', '.join(against)}"
    blind = [e.ref for e in ledger.by_kind(*FAILED, INCOMPLETE)]
    if blind:
        return False, f"claims health while {', '.join(blind[:4])} could not be fully collected"
    return True, None


def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE.split(text or "") if s.strip()]
