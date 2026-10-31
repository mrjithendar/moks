"""Skill-driven detection: deterministic rules decide when an incident exists.

Each rule reads live evidence on its schedule through the ToolExecutor, as the automation principal.
A check has one of three results, and the difference matters:

  firing   the rule's condition holds for these resources
  ok       the source answered and the condition does not hold
  unknown  the source failed, or returned nothing the rule cannot interpret

Unknown changes nothing: missing telemetry is neither healthy nor a failure, so it cannot resolve an
incident or open one. A telemetry-health rule can watch the telemetry itself.

A check is also complete or not. Recovery is only inferred for what a complete, successful read
actually covered: a namespace that failed, was truncated, or fell outside this run's slice proves
nothing about the resources in it.

An incident's identity is sha256(rule, environment, namespace, kind, resource), using the owning
workload rather than the pod, so restarts do not open new incidents. Repeats update it; a partial
unique index makes that hold across workers. Rules never name actions: an automatic incident authorizes
no change. An LLM takes no part in these decisions.
"""
from __future__ import annotations

import hashlib
import logging
import operator
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit, skills
from .connectors import registry
from .skills.registry import allowed as registry_allowed
from .skills.registry import policy as skill_policy
from .skills.registry import refresh as refresh_skills
from .connectors.base import ConnectorError
from .db import session_scope
from .incidents import add_event
from .models import (AUTOMATION_USER_ID, DetectionPolicy, DetectionRun, DetectionState, Environment, Incident, Investigation,
                     InvestigationEvent, MaintenanceWindow)
from .rbac import P
from .scope import ScopeError, effective_scope
from .tools.contracts import ToolContext
from .tools.executor import ToolExecutor

log = logging.getLogger("moks.detection")
OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "==": operator.eq, "!=": operator.ne}
MAX_NAMESPACES = 40
MAX_SIGNALS = 50
MAX_SERIES_READ = 5000  # the Prometheus connector's own cap
LOKI_LIMIT = 500
CLOUDWATCH_PAGE = 100
MAX_AUTO_PER_HOUR = 6
ACTIVE = ("open", "mitigated")
RULE_TOOL_CONNECTOR = {"finding": "kubernetes", "prometheus": "prometheus", "prometheus_alert": "prometheus", "loki": "loki", "cloudwatch_alarm": "cloudwatch"}


@dataclass
class Signal:
    resource: str  # stable identity: the owning workload when there is one
    namespace: str | None
    detail: str
    value: float | None = None
    evidence: list[dict] = field(default_factory=list)
    kind: str = ""
    aliases: list[str] = field(default_factory=list)  # names a legacy incident may have used

    def __post_init__(self) -> None:
        if not self.kind:
            self.kind = self.resource.split("/", 1)[0].lower() if "/" in self.resource else "series"


@dataclass
class Check:
    status: str  # firing | ok | unknown
    signals: list[Signal] = field(default_factory=list)
    reason: str | None = None
    complete: bool = True  # everything in scope was read in full
    covered: set[str] = field(default_factory=set)  # when incomplete: namespaces that were read in full
    next_cursor: int = 0

    def proves_absent(self, namespace: str | None) -> bool:
        return self.complete or (namespace is not None and namespace in self.covered)


@dataclass
class Rule:
    spec: skills.DetectionSpec
    skill: skills.Skill

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def version(self) -> str:
        return self.skill.meta.version


def all_rules() -> list[Rule]:
    return [Rule(d, sk) for sk in skills.library().skills.values() for d in sk.meta.detection]


def blocked(rule: Rule, env: Environment, view) -> str | None:
    """A rule runs only while its skill may run: the same answer investigations get."""
    ok, why = registry_allowed(rule.skill, env.name, view)
    return None if ok else why


def effective(rule: Rule, policy: DetectionPolicy | None) -> skills.DetectionSpec:
    """Overrides are validated by the same schema as the skill, so they cannot widen what a rule is."""
    data = rule.spec.model_dump()
    if policy:
        allowed = {"interval_minutes", "lookback_minutes", "sustained_checks", "recovery_checks", "severity", "threshold", "cooldown_minutes",
                   "owner", "auto_investigate", "auto_resolve", "namespaces"}
        data.update({k: v for k, v in (policy.overrides or {}).items() if k in allowed})
        data["enabled"] = policy.enabled
    return skills.DetectionSpec.model_validate(data)


def fingerprint(rule_id: str, env_id: uuid.UUID, namespace: str | None, kind: str, resource: str) -> str:
    return hashlib.sha256("\0".join(["v2", rule_id, str(env_id), namespace or "", kind, resource]).encode()).hexdigest()[:40]


def _stable_owner(owner: str | None) -> str | None:
    """ReplicaSet names change with each rollout; the Deployment does not."""
    if owner and owner.startswith("ReplicaSet/") and "-" in owner:
        return "Deployment/" + owner.split("/", 1)[1].rsplit("-", 1)[0]
    return owner


def _in_scope(ns: str | None, patterns: list[str]) -> bool:
    from fnmatch import fnmatchcase

    return ns is None or any(fnmatchcase(ns, p) for p in patterns)


def _label_resource(labels: dict, keys: list[str]) -> tuple[str, str | None]:
    ns = labels.get("namespace")
    parts = [f"{k}={labels[k]}" for k in (keys or sorted(labels)) if k in labels and k != "__name__"]
    return ",".join(parts) or "cluster", ns


async def _evaluate_finding(spec, ex: ToolExecutor, cursor: int = 0) -> Check:
    k = ex.ctx.connectors.get("kubernetes")
    if k is None:
        return Check("unknown", reason="no kubernetes connector")
    try:
        all_namespaces = sorted(n for n in ex.ctx.scope.filter(await k[1].list_namespaces()) if _in_scope(n, spec.namespaces))
    except ConnectorError as exc:
        return Check("unknown", reason=exc.message)
    total = len(all_namespaces)
    start = cursor % total if total else 0
    namespaces = (all_namespaces[start:] + all_namespaces[:start])[:MAX_NAMESPACES]  # rotate so none is starved
    signals, covered, failed = [], set(), 0
    for ns in namespaces:
        out = await ex.run("k8s_triage", {"namespace": ns})
        if not out.ok:
            failed += 1
            continue
        if not out.truncated and not out.data.get("pods_truncated"):
            covered.add(ns)
        for f in out.data.get("findings", []):
            if f.get("kind") == spec.finding:
                raw = (f.get("resources") or ["unknown"])[0]
                signals.append(Signal(_stable_owner(f.get("owner")) or raw, ns, f.get("title", spec.finding), aliases=[raw],
                                      evidence=[{"tool": "k8s_triage", "summary": f.get("detail", "")[:300], "collected_at": out.collected_at}]))
    if failed == len(namespaces) and namespaces:
        return Check("unknown", reason="no namespace could be read", complete=False)
    complete = len(covered) == total
    return Check("firing" if signals else "ok", signals, complete=complete, covered=covered, next_cursor=(start + len(namespaces)) % max(total, 1),
                 reason=None if complete else f"read {len(covered)} of {total} namespaces in full")


async def _evaluate_prometheus(spec, ex: ToolExecutor) -> Check:
    out = await ex.run("prom_query", {"promql": spec.query, "mode": "instant"})
    if not out.ok:
        return Check("unknown", reason=out.summary)
    series = out.data.get("all_series") or out.data.get("series") or []
    complete = not out.truncated and len(series) < MAX_SERIES_READ
    if not series:
        return Check("ok" if spec.empty_means_ok else "unknown", reason=None if spec.empty_means_ok else "the query returned no data")
    cmp = OPS[spec.operator]
    signals = []
    for s in series:
        value = (s.get("value") or [None, None])[1]
        if value is None or not cmp(float(value), spec.threshold):
            continue
        resource, ns = _label_resource(s.get("metric") or {}, spec.resource_label)
        if _in_scope(ns, spec.namespaces) and (ns is None or ex.ctx.scope.allows(ns)):
            signals.append(Signal(resource, ns, f"value {value} {spec.operator} {spec.threshold}", float(value),
                                  [{"tool": "prom_query", "query": spec.query, "value": value, "collected_at": out.collected_at}]))
    return Check("firing" if signals else "ok", signals, complete=complete, reason=None if complete else "the query result was truncated")


async def _evaluate_alert(spec, ex: ToolExecutor) -> Check:
    out = await ex.run("prom_alerts", {"firing_only": True})
    if not out.ok:
        return Check("unknown", reason=out.summary)
    signals = []
    pattern = re.compile(spec.query)
    for rule in out.data:
        if not pattern.search(rule.get("name", "")):
            continue
        for a in rule.get("active", []):
            if a.get("state") == "firing":
                resource, ns = _label_resource(a.get("labels") or {}, spec.resource_label or ["alertname", "service", "pod"])
                signals.append(Signal(resource, ns, (rule.get("annotations") or {}).get("summary") or rule["name"],
                                      evidence=[{"tool": "prom_alerts", "alert": rule["name"], "collected_at": out.collected_at}]))
    return Check("firing" if signals else "ok", signals, complete=not out.truncated)


async def _evaluate_loki(spec, ex: ToolExecutor) -> Check:
    out = await ex.run("loki_logs", {"contains": spec.query, "minutes": spec.lookback_minutes, "limit": LOKI_LIMIT})
    if not out.ok:
        return Check("unknown", reason=out.summary)
    lines = out.data.get("lines") or []
    if not lines:
        return Check("ok" if spec.empty_means_ok else "unknown", reason=None if spec.empty_means_ok else "no log lines to judge from")
    counts: dict[tuple, int] = {}
    for line in lines:
        labels = line.get("labels") or {}
        key = (labels.get("namespace"), labels.get("app") or labels.get("service") or labels.get("pod") or "logs")
        counts[key] = counts.get(key, 0) + 1
    cmp = OPS[spec.operator]
    signals = [Signal(str(name), ns, f"{n} matching lines in {spec.lookback_minutes} min", n, [{"tool": "loki_logs", "contains": spec.query, "lines": n, "collected_at": out.collected_at}])
               for (ns, name), n in counts.items() if cmp(n, spec.threshold) and _in_scope(ns, spec.namespaces)]
    return Check("firing" if signals else "ok", signals, complete=not out.truncated and len(lines) < LOKI_LIMIT)


async def _evaluate_cloudwatch(spec, ex: ToolExecutor) -> Check:
    out = await ex.run("cw_alarms", {"state": "ALARM"})
    if not out.ok:
        return Check("unknown", reason=out.summary)
    pattern = re.compile(spec.query)
    signals = [Signal(a.get("name", "alarm"), None, a.get("reason") or a.get("description") or "alarm", evidence=[{"tool": "cw_alarms", "alarm": a.get("name"), "collected_at": out.collected_at}])
               for a in out.data if pattern.search(a.get("name", ""))]
    return Check("firing" if signals else "ok", signals, complete=not out.truncated and len(out.data) < CLOUDWATCH_PAGE)


EVALUATORS = {"finding": _evaluate_finding, "prometheus": _evaluate_prometheus, "prometheus_alert": _evaluate_alert, "loki": _evaluate_loki, "cloudwatch_alarm": _evaluate_cloudwatch}


async def _executor(db: AsyncSession, env: Environment, spec, principal=None) -> ToolExecutor | None:
    from .alerts import automation_principal

    principal = principal or await automation_principal(db)
    try:
        scope = effective_scope(principal, env, P.INVESTIGATION_RUN)
    except ScopeError:
        return None
    connectors, _ = await registry.for_environment(db, env.id)
    now = datetime.now(timezone.utc)
    ctx = ToolContext(None, env, scope, principal, now - timedelta(minutes=spec.lookback_minutes), now, connectors, timedelta(minutes=max(spec.lookback_minutes, 60)))
    return ToolExecutor(None, ctx, timeout_seconds=30, read_permissions=(P.INVESTIGATION_RUN,), audit_success=False)


async def evaluate(db: AsyncSession, env: Environment, spec, principal=None, cursor: int = 0) -> Check:
    ex = await _executor(db, env, spec, principal)
    if ex is None:
        return Check("unknown", reason="no access to this environment")
    needed = RULE_TOOL_CONNECTOR[spec.kind]
    if needed not in ex.ctx.connectors:
        return Check("unknown", reason=f"no {needed} connector")
    try:
        check = await (_evaluate_finding(spec, ex, cursor) if spec.kind == "finding" else EVALUATORS[spec.kind](spec, ex))
    except re.error:
        return Check("unknown", reason="the rule's pattern is invalid", complete=False)
    unique: dict[tuple, Signal] = {}  # pods of one workload are one resource
    for sig in check.signals:
        seen = unique.setdefault((sig.namespace, sig.kind, sig.resource), sig)
        if seen is not sig:
            seen.aliases.extend(sig.aliases)
            seen.evidence.extend(sig.evidence[:1])
    check.signals = sorted(unique.values(), key=lambda x: (x.namespace or "", x.resource))
    if len(check.signals) > MAX_SIGNALS:  # the rest were not looked at, so nothing can be concluded about them
        check.signals, check.complete, check.covered = check.signals[:MAX_SIGNALS], False, set()
        check.reason = f"more than {MAX_SIGNALS} resources matched"
    if check.status == "firing" and sum(len(s.evidence) for s in check.signals) < spec.min_evidence:
        return Check("unknown", reason=f"needs {spec.min_evidence} pieces of evidence")
    return check


async def _suppressed(db: AsyncSession, env_id: uuid.UUID, rule_id: str, namespace: str | None, now: datetime) -> MaintenanceWindow | None:
    rows = (await db.execute(select(MaintenanceWindow).where(MaintenanceWindow.environment_id == env_id, MaintenanceWindow.starts_at <= now,
                                                             MaintenanceWindow.ends_at > now))).scalars().all()
    return next((w for w in rows if w.rule_id in ("*", rule_id) and w.namespace in ("*", namespace)), None)


async def _active_incident(db: AsyncSession, env_id: uuid.UUID, fp: str) -> Incident | None:
    return (await db.execute(select(Incident).where(Incident.environment_id == env_id, Incident.fingerprint == fp, Incident.source == "detection",
                                                    Incident.status.in_(ACTIVE)))).scalars().first()


async def _recently_closed(db: AsyncSession, env_id: uuid.UUID, fp: str, cooldown: int, now: datetime) -> Incident | None:
    if not cooldown:
        return None
    return (await db.execute(select(Incident).where(Incident.environment_id == env_id, Incident.fingerprint == fp, Incident.source == "detection",
                                                    Incident.status == "resolved", Incident.resolved_at > now - timedelta(minutes=cooldown))
                             .order_by(Incident.resolved_at.desc()))).scalars().first()


def _title(rule: Rule, spec, sig: Signal) -> str:
    return f"{spec.title or rule.skill.meta.title or rule.id}: {sig.resource}"[:300]


async def _open_incident(db: AsyncSession, env: Environment, rule: Rule, spec, sig: Signal, st: DetectionState, now: datetime) -> Incident:
    reason = f"{spec.severity} set by rule {rule.id} (skill {rule.skill.name} v{rule.version}): {st.consecutive} consecutive checks at {spec.interval_minutes} min intervals."
    inc = Incident(title=_title(rule, spec, sig), severity=spec.severity, status="open", environment_id=env.id, namespace=sig.namespace or "*",
                   summary=sig.detail[:2000], source="detection", fingerprint=st.fingerprint, created_by=AUTOMATION_USER_ID, rule_id=rule.id,
                   rule_version=rule.version, skill=rule.skill.name, skill_digest=rule.skill.digest, resource=sig.resource[:300], first_observed_at=st.first_seen, last_observed_at=now,
                   detection_count=st.consecutive, owner_team=spec.owner or rule.skill.meta.owner, severity_reason=reason,
                   runbook=", ".join(rule.skill.meta.runbooks) or f"skill {rule.skill.name}")
    try:
        async with db.begin_nested():
            db.add(inc)
            await db.flush()
    except IntegrityError:  # another worker opened it first
        existing = await _active_incident(db, env.id, st.fingerprint)
        if existing is None:
            raise
        return existing
    await add_event(db, inc.id, "detection", {"text": f"Opened by rule {rule.id}: {sig.detail}", "rule": rule.id, "rule_version": rule.version, "skill": rule.skill.name,
                                              "evidence": sig.evidence, "resource": sig.resource})
    await audit.record(db, action="detection.incident", actor_id=AUTOMATION_USER_ID, actor_email="automation@moks.system", resource_type="incident",
                       resource_id=str(inc.id), environment=env.name, namespace=sig.namespace, decision="allow", details={"rule": rule.id, "version": rule.version, "fingerprint": st.fingerprint})
    if spec.auto_investigate:
        await _investigate(db, env, inc, rule, sig, now)
    return inc


async def _investigate(db: AsyncSession, env: Environment, inc: Incident, rule: Rule, sig: Signal, now: datetime) -> None:
    from sqlalchemy import func

    count = (await db.execute(select(func.count()).select_from(Investigation).where(
        Investigation.environment_id == env.id, Investigation.trigger == "detection", Investigation.created_at > now - timedelta(hours=1)))).scalar_one()
    if count >= MAX_AUTO_PER_HOUR:
        await add_event(db, inc.id, "note", {"text": f"Automatic investigation skipped: {MAX_AUTO_PER_HOUR}/hour cap reached"})
        return
    inv = Investigation(created_by=AUTOMATION_USER_ID, environment_id=env.id, namespace=sig.namespace or "*", incident_id=inc.id, trigger="detection", status="queued",
                        question=f"{inc.title}. {sig.detail}. What is happening and what is the likely cause?"[:2000],
                        time_from=now - timedelta(minutes=max(rule.spec.lookback_minutes, 30)), time_to=now)
    db.add(inv)
    await db.flush()
    db.add(InvestigationEvent(investigation_id=inv.id, seq=0, type="queued", payload={"window_minutes": max(rule.spec.lookback_minutes, 30), "environment": env.name, "namespace": inv.namespace}))
    await add_event(db, inc.id, "investigation", {"text": "Automatic investigation started", "investigation_id": str(inv.id)})


async def _adopt_legacy(db: AsyncSession, env: Environment, rule: Rule, sig: Signal, fp: str) -> None:
    """Open incidents from before per-namespace identity keep their history."""
    names = {sig.resource, *sig.aliases}
    rows = (await db.execute(select(Incident).where(Incident.environment_id == env.id, Incident.source == "detection", Incident.rule_id == rule.id,
                                                    Incident.status.in_(ACTIVE), Incident.fingerprint.like("v1-%"), Incident.namespace == (sig.namespace or "*")))).scalars().all()
    legacy = next((i for i in rows if i.resource in names), None)
    if legacy is not None and await _active_incident(db, env.id, fp) is None:
        legacy.fingerprint, legacy.resource = fp, sig.resource[:300]
        await add_event(db, legacy.id, "detection", {"text": f"Adopted under the new identity {sig.kind}/{sig.resource}", "rule": rule.id})


def decide(st: DetectionState | None, spec, *, active: bool, reopen: bool, suppressed: bool) -> str:
    """What a firing signal leads to; shared by the real run and the dry run."""
    seen = (st.consecutive if st else 0) + 1
    if suppressed:
        return "suppressed by a maintenance window"
    if active:
        return "update the open incident"
    if seen < spec.sustained_checks:
        return f"wait: {seen} of {spec.sustained_checks} checks"
    return "reopen the recent incident" if reopen else "open an incident"


async def apply(db: AsyncSession, env: Environment, rule: Rule, spec, check: Check, now: datetime) -> dict[str, int]:
    tally = {"opened": 0, "updated": 0, "resolved": 0, "suppressed": 0}
    if check.status == "unknown":
        return tally  # no data is not evidence of anything
    seen: set[str] = set()
    for sig in check.signals:
        fp = fingerprint(rule.id, env.id, sig.namespace, sig.kind, sig.resource)
        seen.add(fp)
        await _adopt_legacy(db, env, rule, sig, fp)
        st = await db.get(DetectionState, fp, with_for_update=True)
        if st is None:
            st = DetectionState(fingerprint=fp, rule_id=rule.id, environment_id=env.id, resource=sig.resource[:300], namespace=sig.namespace, first_seen=now, last_seen=now, consecutive=0, clear_checks=0)
            db.add(st)
        active = await _active_incident(db, env.id, fp)
        closed = None if active else await _recently_closed(db, env.id, fp, spec.cooldown_minutes, now)
        window = await _suppressed(db, env.id, rule.id, sig.namespace, now)
        verdict = decide(st, spec, active=active is not None, reopen=closed is not None, suppressed=window is not None)
        st.consecutive += 1
        st.clear_checks, st.last_seen = 0, now
        if st.status == "cleared":
            st.first_seen = now
        if verdict.startswith("suppressed"):
            st.status = "suppressed"
            tally["suppressed"] += 1
            continue
        st.status = "firing" if st.consecutive >= spec.sustained_checks or active else "pending"
        if active:
            active.last_observed_at, active.detection_count = now, active.detection_count + 1
            st.incident_id = active.id
            tally["updated"] += 1
        elif verdict.startswith("reopen"):
            closed.status, closed.resolved_at, closed.last_observed_at = "open", None, now
            closed.detection_count += 1
            await add_event(db, closed.id, "detection", {"text": f"Reopened: the condition returned within the {spec.cooldown_minutes} min cooldown (flapping)", "rule": rule.id})
            st.incident_id = closed.id
            tally["updated"] += 1
        elif verdict == "open an incident":
            inc = await _open_incident(db, env, rule, spec, sig, st, now)
            st.incident_id = inc.id
            tally["opened"] += 1
    states = (await db.execute(select(DetectionState).where(DetectionState.rule_id == rule.id, DetectionState.environment_id == env.id,
                                                            DetectionState.status.in_(["pending", "firing", "suppressed"])).with_for_update())).scalars().all()
    for st in states:
        if st.fingerprint in seen or not check.proves_absent(st.namespace):
            continue  # not seen firing, but also not seen clear
        st.consecutive, st.clear_checks = 0, st.clear_checks + 1
        if st.clear_checks < spec.recovery_checks:
            continue
        st.status = "cleared"
        inc = await db.get(Incident, st.incident_id) if st.incident_id else None
        if inc is None or inc.status not in ACTIVE:
            continue
        await add_event(db, inc.id, "detection", {"text": f"Condition clear for {st.clear_checks} consecutive checks", "rule": rule.id})
        if spec.auto_resolve:
            inc.status, inc.resolved_at = "resolved", now
            tally["resolved"] += 1
            await audit.record(db, action="detection.resolve", actor_id=AUTOMATION_USER_ID, actor_email="automation@moks.system", resource_type="incident",
                               resource_id=str(inc.id), environment=env.name, decision="allow", details={"rule": rule.id, "clear_checks": st.clear_checks})
    return tally


def applies(spec, env: Environment, policy: DetectionPolicy | None) -> bool:
    from fnmatch import fnmatchcase

    if not spec.enabled or not any(fnmatchcase(env.name, p) for p in spec.environments):
        return False
    return not (policy and policy.environments) or env.name in policy.environments


async def tick(env_id: uuid.UUID) -> dict[str, Any]:
    """Run the rules that are due. Timing lives in the database, so a restart loses nothing."""
    now = datetime.now(timezone.utc)
    out = {"checked": 0, "unknown": 0, "opened": 0, "updated": 0, "resolved": 0, "suppressed": 0}
    async with session_scope() as db:
        env = await db.get(Environment, env_id)
        if env is None or not env.detection_enabled:
            return {"skipped": "detection is off for this environment"}
        policies = {p.rule_id: p for p in (await db.execute(select(DetectionPolicy))).scalars()}
        runs = {r.rule_id: r for r in (await db.execute(select(DetectionRun).where(DetectionRun.environment_id == env_id))).scalars()}
        view = await skill_policy(db)
        environment = env
    lib = await refresh_skills()
    for rule in all_rules():
        if blocked(rule, environment, view):
            out["blocked"] = out.get("blocked", 0) + 1
            continue
        policy = policies.get(rule.id)
        try:
            spec = effective(rule, policy)
        except ValueError:
            log.warning("invalid detection override", extra={"event": "detection_override_invalid"})
            continue
        run = runs.get(rule.id)
        if not applies(spec, env, policy) or (run and now - run.last_run_at < timedelta(minutes=spec.interval_minutes)):
            continue
        async with session_scope() as db:
            env = await db.get(Environment, env_id)
            check = await evaluate(db, env, spec, cursor=run.cursor if run else 0)
            tally = await apply(db, env, rule, spec, check, now)
            await db.merge(DetectionRun(rule_id=rule.id, environment_id=env_id, last_run_at=now, last_status=check.status, last_reason=check.reason, cursor=check.next_cursor,
                                        skill=rule.skill.name, skill_version=rule.version, skill_digest=rule.skill.digest, skill_generation=lib.generation))
        out["checked"] += 1
        out["unknown"] += check.status == "unknown"
        for k, v in tally.items():
            out[k] += v
    return out


async def dry_run(db: AsyncSession, env: Environment, rule: Rule, policy: DetectionPolicy | None, principal) -> dict[str, Any]:
    """Runs the check with the caller's own access, so a rule can be tried before detection is
    turned on. Writes nothing and notifies no one."""
    why = blocked(rule, env, await skill_policy(db))
    if why:
        return {"rule": rule.id, "version": rule.version, "environment": env.name, "status": "blocked", "reason": f"skill {rule.skill.name}: {why}", "partial": False, "preview": [],
                "auto_investigate": False, "note": "A blocked rule does not run, in a dry run or on schedule."}
    spec = effective(rule, policy)
    now = datetime.now(timezone.utc)
    check = await evaluate(db, env, spec, principal)
    preview = []
    for sig in check.signals:
        fp = fingerprint(rule.id, env.id, sig.namespace, sig.kind, sig.resource)
        st = await db.get(DetectionState, fp)
        active = await _active_incident(db, env.id, fp)
        closed = None if active else await _recently_closed(db, env.id, fp, spec.cooldown_minutes, now)
        window = await _suppressed(db, env.id, rule.id, sig.namespace, now)
        preview.append({"resource": sig.resource, "namespace": sig.namespace, "detail": sig.detail, "title": _title(rule, spec, sig), "severity": spec.severity,
                        "would": decide(st, spec, active=active is not None, reopen=closed is not None, suppressed=window is not None), "evidence": sig.evidence})
    return {"rule": rule.id, "version": rule.version, "digest": rule.skill.digest, "environment": env.name, "status": check.status, "reason": check.reason, "partial": not check.complete,
            "preview": preview, "auto_investigate": spec.auto_investigate, "note": "An incident never authorizes a change; remediation stays behind action plans and approvals."}
