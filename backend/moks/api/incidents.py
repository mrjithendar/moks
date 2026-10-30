"""Incident workspaces: create, timeline, hypotheses, link investigations, status, report, postmortem."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..auth.deps import get_auth, require
from ..auth.sessions import AuthContext
from ..config import get_settings
from ..db import get_db
from ..incidents import add_event, incident_detail, report_markdown
from ..models import Environment, Incident, Investigation
from ..rbac import P
from .common import env_or_404

router = APIRouter(prefix="/incidents", tags=["incidents"])


class IncidentIn(BaseModel):
    title: str = Field(min_length=3, max_length=300)
    environment: str
    namespace: str = Field(default="*", pattern=r"^(\*|[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)$")
    severity: Literal["sev1", "sev2", "sev3", "sev4"] = "sev3"
    summary: str = Field(default="", max_length=5000)


class UpdateIn(BaseModel):
    status: Literal["open", "mitigated", "resolved", "closed"] | None = None
    severity: Literal["sev1", "sev2", "sev3", "sev4"] | None = None
    summary: str | None = Field(default=None, max_length=5000)
    hypotheses: list[dict[str, Any]] | None = Field(default=None, max_length=30)


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    kind: Literal["note", "evidence", "hypothesis", "validation", "communication"] = "note"
    evidence_ref: str | None = Field(default=None, max_length=200)


class PostmortemIn(BaseModel):
    impact: str = Field(default="", max_length=5000)
    root_cause: str = Field(default="", max_length=5000)
    root_cause_verified: bool = False
    contributing_factors: str = Field(default="", max_length=5000)
    detection: str = Field(default="", max_length=3000)
    resolution: str = Field(default="", max_length=3000)
    lessons: str = Field(default="", max_length=5000)
    action_items: list[str] = Field(default_factory=list, max_length=30)


async def _load(request: Request, db: AsyncSession, ctx: AuthContext, iid: uuid.UUID, write: bool = False) -> tuple[Incident, Environment]:
    inc = await db.get(Incident, iid)
    if inc is None:
        raise HTTPException(404, "Incident not found")
    env = await db.get(Environment, inc.environment_id)
    if not ctx.principal.can(P.INVESTIGATION_READ, env.name, inc.namespace):
        raise HTTPException(404, "Incident not found")
    if write:
        await require(request, ctx, P.INCIDENT_WRITE, env.name, inc.namespace)
    return inc, env


@router.get("")
async def list_incidents(status: str | None = None, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict]:
    q = select(Incident, Environment.name).join(Environment).order_by(Incident.created_at.desc()).limit(300)
    if status:
        q = q.where(Incident.status == status)
    out = []
    for inc, env_name in (await db.execute(q)).all():
        if ctx.principal.can(P.INVESTIGATION_READ, env_name, inc.namespace):
            out.append({"id": str(inc.id), "title": inc.title, "severity": inc.severity, "status": inc.status, "environment": env_name,
                        "namespace": inc.namespace, "source": inc.source, "created_at": inc.created_at.isoformat(),
                        "updated_at": inc.updated_at.isoformat(), "postmortem_status": inc.postmortem_status})
    return out


@router.post("", status_code=201)
async def create(body: IncidentIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    env = await env_or_404(db, body.environment)
    await require(request, ctx, P.INCIDENT_WRITE, env.name, body.namespace)
    inc = Incident(title=body.title, environment_id=env.id, namespace=body.namespace, severity=body.severity, summary=body.summary,
                   created_by=ctx.user.id, commander_id=ctx.user.id)
    db.add(inc)
    await db.flush()
    await add_event(db, inc.id, "status", {"text": f"Incident opened ({body.severity})", "status": "open"}, ctx.user.id)
    await audit.record(db, action="incident.create", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="incident",
                       resource_id=str(inc.id), environment=env.name, namespace=body.namespace)
    return await incident_detail(db, inc)


@router.get("/{iid}")
async def get(iid: uuid.UUID, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    inc, _ = await _load(request, db, ctx, iid)
    return await incident_detail(db, inc)


@router.patch("/{iid}")
async def update(iid: uuid.UUID, body: UpdateIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    inc, env = await _load(request, db, ctx, iid, write=True)
    changes = body.model_dump(exclude_none=True)
    if body.status and body.status != inc.status:
        await add_event(db, inc.id, "status", {"text": f"Status {inc.status} -> {body.status}", "status": body.status}, ctx.user.id)
        if body.status in ("resolved", "closed") and not inc.resolved_at:
            inc.resolved_at = datetime.now(timezone.utc)
    if body.hypotheses is not None:
        clean = [{"text": str(h.get("text", ""))[:1000], "confidence": h.get("confidence", "low") if h.get("confidence") in ("low", "medium", "high") else "low",
                  "status": h.get("status", "open") if h.get("status") in ("open", "supported", "refuted", "verified") else "open",
                  "evidence": [str(e)[:200] for e in (h.get("evidence") or [])][:10]} for h in body.hypotheses]
        changes["hypotheses"] = clean
        await add_event(db, inc.id, "hypothesis", {"text": f"{len(clean)} hypotheses updated"}, ctx.user.id)
    for k, v in changes.items():
        setattr(inc, k, v)
    await audit.record(db, action="incident.update", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="incident",
                       resource_id=str(inc.id), environment=env.name, namespace=inc.namespace, details={"fields": sorted(changes)})
    return await incident_detail(db, inc)


@router.post("/{iid}/events", status_code=201)
async def add_note(iid: uuid.UUID, body: NoteIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    inc, _ = await _load(request, db, ctx, iid, write=True)
    await add_event(db, inc.id, body.kind, {"text": body.text, "evidence_ref": body.evidence_ref}, ctx.user.id)
    await db.flush()
    return await incident_detail(db, inc)


@router.post("/{iid}/investigations/{inv_id}")
async def link_investigation(iid: uuid.UUID, inv_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(get_auth),
                             db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    inc, env = await _load(request, db, ctx, iid, write=True)
    inv = await db.get(Investigation, inv_id)
    if inv is None or inv.environment_id != inc.environment_id or not ctx.principal.can(P.INVESTIGATION_READ, env.name, inv.namespace):
        raise HTTPException(404, "Investigation not found in this incident's environment")
    inv.incident_id = inc.id
    await add_event(db, inc.id, "investigation", {"text": f"Investigation linked: {inv.question[:200]}", "investigation_id": str(inv.id)}, ctx.user.id)
    await db.flush()
    return await incident_detail(db, inc)


@router.get("/{iid}/report.md", response_class=PlainTextResponse)
async def report(iid: uuid.UUID, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> str:
    inc, _ = await _load(request, db, ctx, iid)
    return report_markdown(await incident_detail(db, inc))


@router.put("/{iid}/postmortem")
async def save_postmortem(iid: uuid.UUID, body: PostmortemIn, request: Request, ctx: AuthContext = Depends(get_auth),
                          db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    inc, _ = await _load(request, db, ctx, iid, write=True)
    if inc.postmortem_status == "approved":
        raise HTTPException(409, "Approved postmortems are final")
    inc.postmortem, inc.postmortem_status = body.model_dump(), "draft"
    await add_event(db, inc.id, "note", {"text": "Postmortem draft saved"}, ctx.user.id)
    return await incident_detail(db, inc)


@router.post("/{iid}/postmortem/approve")
async def approve_postmortem(iid: uuid.UUID, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    from ..rag import ingest

    inc, env = await _load(request, db, ctx, iid)
    await require(request, ctx, P.ACTION_APPROVE, env.name, inc.namespace)
    if inc.postmortem_status != "draft":
        raise HTTPException(409, "Save a postmortem draft first")
    if inc.status not in ("resolved", "closed"):
        raise HTTPException(409, "Resolve the incident before approving its postmortem")
    inc.postmortem_status, inc.postmortem_approved_by = "approved", ctx.user.id
    md = report_markdown(await incident_detail(db, inc))
    doc = await ingest.add_postmortem(db, inc, env.name, md, get_settings().public_url)
    await add_event(db, inc.id, "note", {"text": "Postmortem approved and added to the knowledge base", "document": doc.path}, ctx.user.id)
    await audit.record(db, action="incident.postmortem_approve", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="incident",
                       resource_id=str(inc.id), environment=env.name, namespace=inc.namespace)
    return await incident_detail(db, inc)
