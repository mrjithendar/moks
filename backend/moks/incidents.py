"""Incident workspaces: timeline, linked investigations, hypotheses, actions, validation,
incident reports and postmortems (approved postmortems feed the knowledge base)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ActionPlan, Environment, Incident, IncidentEvent, Investigation, User


async def add_event(db: AsyncSession, incident_id: uuid.UUID, kind: str, payload: dict[str, Any], actor_id=None) -> None:
    db.add(IncidentEvent(incident_id=incident_id, kind=kind, payload=payload, actor_id=actor_id))
    inc = await db.get(Incident, incident_id)
    if inc:
        inc.updated_at = datetime.now(timezone.utc)


SEVERITY_FROM_LABEL = {"critical": "sev2", "page": "sev2", "error": "sev3", "warning": "sev3", "info": "sev4", "sev1": "sev1",
                       "sev2": "sev2", "sev3": "sev3", "sev4": "sev4"}


async def incident_detail(db: AsyncSession, inc: Incident) -> dict[str, Any]:
    env = await db.get(Environment, inc.environment_id)
    events = (await db.execute(select(IncidentEvent).where(IncidentEvent.incident_id == inc.id).order_by(IncidentEvent.ts))).scalars().all()
    invs = (await db.execute(select(Investigation).where(Investigation.incident_id == inc.id).order_by(Investigation.created_at))).scalars().all()
    plans = (await db.execute(select(ActionPlan).where(ActionPlan.incident_id == inc.id).order_by(ActionPlan.created_at))).scalars().all()
    actors = {u.id: u.email for u in (await db.execute(select(User).where(User.id.in_({e.actor_id for e in events if e.actor_id} |
                                                                                       ({inc.created_by} if inc.created_by else set()))))).scalars()}
    return {
        "id": str(inc.id), "title": inc.title, "severity": inc.severity, "status": inc.status, "environment": env.name if env else None,
        "namespace": inc.namespace, "summary": inc.summary, "hypotheses": inc.hypotheses, "source": inc.source,
        "created_by": actors.get(inc.created_by), "created_at": inc.created_at.isoformat(), "updated_at": inc.updated_at.isoformat(),
        "resolved_at": inc.resolved_at.isoformat() if inc.resolved_at else None,
        "commander": (await db.get(User, inc.commander_id)).email if inc.commander_id else None,
        "detection": {"rule": inc.rule_id, "rule_version": inc.rule_version, "skill": inc.skill, "resource": inc.resource, "owner": inc.owner_team, "severity_reason": inc.severity_reason,
                      "runbook": inc.runbook, "count": inc.detection_count, "first_observed": inc.first_observed_at.isoformat() if inc.first_observed_at else None,
                      "last_observed": inc.last_observed_at.isoformat() if inc.last_observed_at else None} if inc.rule_id else None,
        "postmortem": inc.postmortem, "postmortem_status": inc.postmortem_status,
        "timeline": [{"id": e.id, "ts": e.ts.isoformat(), "kind": e.kind, "actor": actors.get(e.actor_id), "payload": e.payload} for e in events],
        "investigations": [{"id": str(i.id), "question": i.question, "status": i.status, "namespace": i.namespace, "trigger": i.trigger,
                            "created_at": i.created_at.isoformat(), "summary": (i.report or {}).get("summary"),
                            "causes": (i.report or {}).get("suspected_causes", [])[:3]} for i in invs],
        "actions": [{"id": str(p.id), "action": p.action, "target": f"{p.target_kind}/{p.target_name}", "namespace": p.namespace,
                     "status": p.status, "created_at": p.created_at.isoformat(), "result": p.result} for p in plans],
    }


def report_markdown(d: dict[str, Any]) -> str:
    """Incident report from recorded data only: nothing is invented."""
    pm = d.get("postmortem") or {}
    lines = [f"# Incident: {d['title']}", "",
             f"- Severity: {d['severity']}  ·  Status: {d['status']}  ·  Scope: {d['environment']}/{d['namespace']}",
             f"- Opened: {d['created_at']} by {d['created_by'] or 'automation'}" + (f"  ·  Resolved: {d['resolved_at']}" if d.get("resolved_at") else ""),
             f"- Postmortem: {d['postmortem_status']}", ""]
    if d.get("summary"):
        lines += ["## Summary", "", d["summary"], ""]
    if pm:
        for key, title in (("impact", "Impact"), ("root_cause", "Root cause"), ("contributing_factors", "Contributing factors"),
                           ("detection", "Detection"), ("resolution", "Resolution"), ("lessons", "Lessons learned")):
            if pm.get(key):
                lines += [f"## {title}", "", str(pm[key]), ""]
        if pm.get("root_cause_verified") is False:
            lines += ["> The root cause above has not been verified by a validated change.", ""]
        if pm.get("action_items"):
            lines += ["## Action items", ""] + [f"- [ ] {a}" for a in pm["action_items"]] + [""]
    if d.get("hypotheses"):
        lines += ["## Hypotheses", ""] + [f"- **{h.get('status', 'open')}** ({h.get('confidence', '?')}): {h.get('text')}" for h in d["hypotheses"]] + [""]
    if d["investigations"]:
        lines += ["## Investigations", ""]
        for i in d["investigations"]:
            lines.append(f"- {i['created_at']} [{i['status']}] {i['question']}" + (f" — {i['summary']}" if i.get("summary") else ""))
            for c in i.get("causes") or []:
                lines.append(f"  - suspected ({c.get('confidence')}): {c.get('cause')}")
        lines.append("")
    if d["actions"]:
        lines += ["## Actions", ""]
        for a in d["actions"]:
            res = a.get("result") or {}
            lines.append(f"- {a['created_at']} {a['action']} {a['target']} in {a['namespace']}: **{a['status']}**"
                         + (f" (validation: {res.get('validation', {}).get('ok')})" if res.get("validation") else ""))
        lines.append("")
    lines += ["## Timeline", ""]
    for e in d["timeline"]:
        p = e["payload"]
        text = p.get("text") or p.get("summary") or p.get("message") or p.get("status") or ", ".join(f"{k}={v}" for k, v in list(p.items())[:4])
        lines.append(f"- {e['ts']} · {e['kind']} · {e['actor'] or 'system'}: {text}")
    return "\n".join(lines) + "\n"


async def handover_markdown(db: AsyncSession, inc: Incident) -> str:
    """Facts, suspicions and verified causes stay in separate sections. Everything comes from
    recorded evidence: nothing is summarized by an LLM here."""
    d = await incident_detail(db, inc)
    latest = (await db.execute(select(Investigation).where(Investigation.incident_id == inc.id, Investigation.report.is_not(None))
                               .order_by(Investigation.created_at.desc()))).scalars().first()
    report = (latest.report if latest else None) or {}
    lines = [f"# Handover: {d['title']}", "", f"Severity {d['severity']}, status {d['status']}, environment {d['environment']}/{d['namespace']}.",
             f"Assigned to: {d['commander'] or 'nobody'}.", ""]
    if d["detection"]:
        lines += [f"Detected by rule {d['detection']['rule']} (skill {d['detection']['skill']} v{d['detection']['rule_version']}) on {d['detection']['resource']}.",
                  f"First seen {d['detection']['first_observed']}, last seen {d['detection']['last_observed']}.", ""]

    def section(title: str, rows: list[str], empty: str) -> None:
        lines.extend([f"## {title}", *([f"- {r}" for r in rows] or [empty]), ""])

    section("Observed facts", [f["statement"] for f in report.get("observed_facts", [])][:10], "None recorded.")
    section("Verified causes", [c["cause"] for c in report.get("verified_causes", [])], "None verified.")
    section("Suspected causes (not verified)", [f"{c['cause']} ({c['confidence']} confidence)" for c in report.get("suspected_causes", [])], "None.")
    section("Missing evidence", report.get("missing_evidence", []), "None listed.")
    plans = (await db.execute(select(ActionPlan).where(ActionPlan.incident_id == inc.id).order_by(ActionPlan.created_at))).scalars().all()
    section("Executed actions", [f"{p.action} on {p.target_kind}/{p.target_name}: {p.status}" for p in plans if p.executed_by], "None.")
    section("Validated recovery", [f"{p.action} on {p.target_kind}/{p.target_name}" for p in plans if p.status == "succeeded" and (p.result or {}).get("validation", {}).get("ok")], "Not validated.")
    section("Next steps", report.get("next_steps", []), "None listed.")
    return "\n".join(lines)
