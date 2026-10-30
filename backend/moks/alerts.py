"""Alert-triggered investigations with deduplication, cooldowns and a rate cap.

Inputs: the Alertmanager webhook format (also sent by Grafana alerting contact points) and, where
no Alertmanager exists (mk4), a poller over Prometheus /api/v1/alerts. Each firing alert is
fingerprinted (alertname + identifying labels). Per environment:

- an open incident with the same fingerprint absorbs the alert (deduplicated);
- otherwise, if the fingerprint was investigated within alert_cooldown_minutes -> cooldown;
- otherwise a new incident opens; with auto_investigate, MOKS investigates as the automation
  principal, capped at MAX_AUTO_PER_HOUR per environment (rate_limited beyond that).
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .config import get_settings
from .connectors import registry
from .db import session_scope
from .incidents import SEVERITY_FROM_LABEL, add_event
from .models import AUTOMATION_USER_ID, AlertEvent, Environment, Incident, Investigation, InvestigationEvent, RoleBinding
from .rbac import P, Binding, Principal
from .scope import ScopeError, effective_scope

log = logging.getLogger("moks.alerts")
MAX_AUTO_PER_HOUR = 6
VOLATILE = {"pod", "instance", "pod_template_hash", "endpoint", "job_instance", "container_id", "uid"}


def fingerprint(labels: dict[str, str]) -> str:
    stable = {k: v for k, v in sorted(labels.items()) if k not in VOLATILE}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:32]


def normalize_alertmanager(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for a in (payload.get("alerts") or [])[:200]:
        labels = {str(k): str(v) for k, v in (a.get("labels") or {}).items()}
        if not labels.get("alertname"):
            continue
        out.append({"status": "resolved" if a.get("status") == "resolved" else "firing", "labels": labels,
                    "annotations": {str(k): str(v)[:1000] for k, v in (a.get("annotations") or {}).items()},
                    "starts_at": a.get("startsAt"), "fingerprint": fingerprint(labels)})
    return out


def _parse_ts(v: str | None) -> datetime | None:
    if not v or v.startswith("0001-"):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


async def automation_principal(db: AsyncSession) -> Principal:
    rows = (await db.execute(select(RoleBinding).where(RoleBinding.user_id == AUTOMATION_USER_ID))).scalars().all()
    return Principal(str(AUTOMATION_USER_ID), "automation@moks.system", [Binding(r.role, r.environment, r.namespace) for r in rows])


def question_for(alert: dict[str, Any]) -> str:
    labels, ann = alert["labels"], alert["annotations"]
    what = ann.get("summary") or ann.get("description") or labels["alertname"]
    keys = ", ".join(f"{k}={v}" for k, v in labels.items() if k not in ("alertname", "severity"))[:300]
    return f"Alert {labels['alertname']} is firing: {what}. Labels: {keys}. What is happening and what is the likely cause?"


async def process(env_id: uuid.UUID, alerts: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    decisions = []
    for a in alerts:
        async with session_scope() as db:
            decisions.append(await _process_one(db, env_id, a, source))
    return decisions


async def _process_one(db: AsyncSession, env_id: uuid.UUID, a: dict[str, Any], source: str) -> dict[str, Any]:
    env = await db.get(Environment, env_id)
    now = datetime.now(timezone.utc)
    fp, labels = a["fingerprint"], a["labels"]
    open_inc = (await db.execute(select(Incident).where(Incident.environment_id == env_id, Incident.fingerprint == fp,
                                                        Incident.status.in_(["open", "mitigated"])).order_by(Incident.created_at.desc()))).scalars().first()
    ev = AlertEvent(environment_id=env_id, source=source, fingerprint=fp, alertname=labels["alertname"], status=a["status"],
                    labels=labels, annotations=a["annotations"], starts_at=_parse_ts(a.get("starts_at")), decision="recorded")
    db.add(ev)
    if a["status"] == "resolved":
        ev.decision = "resolved"
        if open_inc:
            ev.incident_id = open_inc.id
            await add_event(db, open_inc.id, "alert", {"text": f"{labels['alertname']} resolved", "status": "resolved", "fingerprint": fp})
        return {"fingerprint": fp, "decision": ev.decision}
    if open_inc:
        ev.decision, ev.incident_id = "deduplicated", open_inc.id
        last = (await db.execute(select(func.max(AlertEvent.received_at)).where(AlertEvent.incident_id == open_inc.id,
                                                                                AlertEvent.status == "firing"))).scalar_one()
        if last is None or now - last > timedelta(minutes=env.alert_cooldown_minutes):
            await add_event(db, open_inc.id, "alert", {"text": f"{labels['alertname']} still firing", "status": "firing", "fingerprint": fp})
        return {"fingerprint": fp, "decision": ev.decision, "incident_id": str(open_inc.id)}
    recent = (await db.execute(select(AlertEvent.id).where(
        AlertEvent.environment_id == env_id, AlertEvent.fingerprint == fp, AlertEvent.decision.in_(["investigated", "incident"]),
        AlertEvent.received_at > now - timedelta(minutes=env.alert_cooldown_minutes)).limit(1))).first()
    if recent:
        ev.decision = "cooldown"
        return {"fingerprint": fp, "decision": ev.decision}
    ns = labels.get("namespace") or labels.get("kubernetes_namespace") or "*"
    inc = Incident(title=f"{labels['alertname']}: {a['annotations'].get('summary', '')}"[:300].rstrip(": "), environment_id=env_id,
                   namespace=ns, severity=SEVERITY_FROM_LABEL.get(labels.get("severity", "").lower(), "sev3"),
                   source="alert-poll" if source == "prometheus-poll" else "alert-webhook",
                   fingerprint=fp, created_by=AUTOMATION_USER_ID, summary=a["annotations"].get("description", "")[:2000])
    db.add(inc)
    await db.flush()
    ev.incident_id, ev.decision = inc.id, "incident"
    await add_event(db, inc.id, "alert", {"text": f"{labels['alertname']} firing ({source})", "labels": labels, "status": "firing"})
    result: dict[str, Any] = {"fingerprint": fp, "decision": "incident", "incident_id": str(inc.id)}
    if env.auto_investigate:
        hour_ago = now - timedelta(hours=1)
        count = (await db.execute(select(func.count()).select_from(Investigation).where(
            Investigation.environment_id == env_id, Investigation.trigger == "alert", Investigation.created_at > hour_ago))).scalar_one()
        if count >= MAX_AUTO_PER_HOUR:
            ev.decision = result["decision"] = "rate_limited"
            await add_event(db, inc.id, "note", {"text": f"Automatic investigation skipped: {MAX_AUTO_PER_HOUR}/hour cap reached"})
        else:
            principal = await automation_principal(db)
            try:
                scope = effective_scope(principal, env, P.INVESTIGATION_RUN)
                if ns != "*":
                    scope.require(ns)
                elif not principal.can(P.INVESTIGATION_RUN, env.name, "*"):
                    raise ScopeError("automation has no all-namespace binding")
            except ScopeError as exc:
                ev.decision = "not_permitted"
                await add_event(db, inc.id, "note", {"text": f"Automatic investigation not permitted: {exc}"})
                await audit.record(db, action="alert.investigate", actor_id=AUTOMATION_USER_ID, actor_email=principal.email,
                                   environment=env.name, namespace=ns, decision="deny", outcome="denied", details={"reason": str(exc)})
                return {**result, "decision": ev.decision}
            window = timedelta(minutes=min(60, env.max_query_window_minutes))
            inv = Investigation(created_by=AUTOMATION_USER_ID, environment_id=env_id, namespace=ns, question=question_for(a),
                                time_from=now - window, time_to=now, status="queued", incident_id=inc.id, trigger="alert")
            db.add(inv)
            await db.flush()
            db.add(InvestigationEvent(investigation_id=inv.id, seq=0, type="queued", payload={"trigger": "alert", "fingerprint": fp}))
            ev.decision, ev.investigation_id = "investigated", inv.id
            await add_event(db, inc.id, "investigation", {"text": "Automatic investigation started", "investigation_id": str(inv.id)})
            result.update(decision="investigated", investigation_id=str(inv.id))
    await audit.record(db, action="alert.received", actor_id=AUTOMATION_USER_ID, actor_email="automation@moks.system",
                       resource_type="incident", resource_id=str(inc.id), environment=env.name, namespace=ns,
                       details={"alertname": labels["alertname"], "fingerprint": fp, "decision": ev.decision, "source": source})
    return result


async def poll_environment(env_id: uuid.UUID) -> dict[str, Any]:
    async with session_scope() as db:
        connectors, errors = await registry.for_environment(db, env_id)
    prom = connectors.get("prometheus")
    if prom is None:
        return {"skipped": errors.get("prometheus", "no prometheus connector")}
    firing = [x for x in await prom[1].active_alerts() if x["state"] == "firing"]
    alerts = [{"status": "firing", "labels": {str(k): str(v) for k, v in x["labels"].items()}, "annotations": x["annotations"],
               "starts_at": x["activeAt"], "fingerprint": fingerprint(x["labels"])} for x in firing if x["labels"].get("alertname")]
    decisions = await process(env_id, alerts, "prometheus-poll")
    # Incidents the poller opened whose alert stopped firing get a "resolved" event, once. Webhook
    # incidents are resolved by their own sender (Alertmanager/Grafana), never by the poller.
    active = {a["fingerprint"] for a in alerts}
    async with session_scope() as db:
        open_alert_incs = (await db.execute(select(Incident).where(Incident.environment_id == env_id, Incident.source == "alert-poll",
                                                                   Incident.status == "open"))).scalars().all()
        already = set((await db.execute(select(AlertEvent.fingerprint).where(
            AlertEvent.environment_id == env_id, AlertEvent.decision == "resolved",
            AlertEvent.incident_id.in_([i.id for i in open_alert_incs] or [uuid.uuid4()])))).scalars())
        resolved = [i for i in open_alert_incs if i.fingerprint and i.fingerprint not in active and i.fingerprint not in already]
    if resolved:
        await process(env_id, [{"status": "resolved", "labels": {"alertname": i.title.split(":")[0]}, "annotations": {},
                                "fingerprint": i.fingerprint} for i in resolved], "prometheus-poll")
    return {"firing": len(alerts), "decisions": [d["decision"] for d in decisions], "resolved": len(resolved)}


def webhook_token_ok(header: str | None) -> bool:
    import hmac

    token = get_settings().alert_webhook_token
    return bool(token) and bool(header) and hmac.compare_digest(header.removeprefix("Bearer ").strip(), token)
