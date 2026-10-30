"""Alert intake: Alertmanager/Grafana webhook (bearer token) and the alert event log."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import alerts as svc
from ..auth.deps import get_auth
from ..auth.ratelimit import limiter
from ..auth.sessions import AuthContext
from ..db import get_db
from ..models import AlertEvent, Environment
from ..rbac import P

router = APIRouter(tags=["alerts"])


@router.post("/alerts/webhook/{environment}")
async def webhook(environment: str, request: Request, authorization: str | None = Header(default=None),
                  db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    if not svc.webhook_token_ok(authorization):
        raise HTTPException(401, "invalid or missing webhook token")
    limiter.check(f"alerts:{environment}", 120)
    env = (await db.execute(select(Environment).where(Environment.name == environment))).scalar_one_or_none()
    if env is None:
        raise HTTPException(404, "unknown environment")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(422, "expected an Alertmanager webhook payload")
    alerts = svc.normalize_alertmanager(body)
    await db.commit()
    decisions = await svc.process(env.id, alerts, "webhook")
    return {"received": len(alerts), "decisions": decisions}


@router.get("/alerts/events")
async def events(environment: str | None = None, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict]:
    q = select(AlertEvent, Environment.name).join(Environment).order_by(AlertEvent.received_at.desc()).limit(200)
    if environment:
        q = q.where(Environment.name == environment)
    out = []
    for e, env_name in (await db.execute(q)).all():
        ns = e.labels.get("namespace", "*")
        if ctx.principal.can(P.INVESTIGATION_READ, env_name, ns):
            out.append({"id": e.id, "environment": env_name, "alertname": e.alertname, "status": e.status, "decision": e.decision,
                        "source": e.source, "labels": e.labels, "annotations": e.annotations, "received_at": e.received_at.isoformat(),
                        "incident_id": str(e.incident_id) if e.incident_id else None,
                        "investigation_id": str(e.investigation_id) if e.investigation_id else None})
    return out
