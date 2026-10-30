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
