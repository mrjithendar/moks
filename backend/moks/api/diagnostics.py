"""Diagnostic sessions: catalog, request (creates an action plan), commands, terminate."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..actions.service import PlanError, plan_out
from ..auth.deps import get_auth
from ..auth.sessions import AuthContext
from ..db import get_db
from ..diagnostics import catalog, service
from ..models import ActionPlan, DiagnosticCommand, DiagnosticSession, Environment
from ..rbac import P
from .common import env_or_404

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


class SessionIn(BaseModel):
    environment: str
    image: str = Field(max_length=40)
    packages: list[str] = Field(default_factory=list, max_length=10)
    ttl_minutes: int = Field(default=30, ge=5, le=240)
    reason: str = Field(min_length=5, max_length=2000)


class CommandIn(BaseModel):
    tool: str = Field(max_length=40)
    args: dict[str, Any] = Field(default_factory=dict)


@router.get("/catalog")
async def get_catalog(ctx: AuthContext = Depends(get_auth)) -> dict:
    return {"images": catalog.IMAGES, "packages": catalog.PACKAGES,
            "tools": {k: {"description": t.description, "args": t.args} for k, t in catalog.TOOLS.items()}}


async def _session_out(db: AsyncSession, s: DiagnosticSession) -> dict:
    env = await db.get(Environment, s.environment_id)
    cmds = (await db.execute(select(DiagnosticCommand).where(DiagnosticCommand.session_id == s.id).order_by(DiagnosticCommand.id))).scalars().all()
    plan = await db.get(ActionPlan, s.plan_id) if s.plan_id else None
    return {"id": str(s.id), "environment": env.name, "image_key": s.image_key, "image": s.image, "packages": s.packages, "elevated": s.elevated,
            "ttl_minutes": s.ttl_minutes, "status": s.status, "pod": s.pod_name, "namespace": s.pod_namespace, "reason": s.reason, "error": s.error,
            "created_at": s.created_at.isoformat(), "expires_at": s.expires_at.isoformat() if s.expires_at else None,
            "plan": plan_out(plan, env.name) if plan else None,
            "commands": [{"id": c.id, "tool": c.tool, "args": c.args, "argv": c.argv, "status": c.status, "exit_code": c.exit_code,
                          "output": c.output, "at": c.created_at.isoformat()} for c in cmds]}


async def _visible(db: AsyncSession, ctx: AuthContext, sid: uuid.UUID) -> DiagnosticSession:
    s = await db.get(DiagnosticSession, sid)
    env = await db.get(Environment, s.environment_id) if s else None
    if s is None or not (s.requested_by == ctx.user.id or ctx.principal.can(P.ACTION_APPROVE, env.name, None) or ctx.principal.can(P.AUDIT_READ, "*", "*")):
        raise HTTPException(404, "Session not found")
    return s


@router.get("/sessions")
async def list_sessions(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict]:
    rows = (await db.execute(select(DiagnosticSession).order_by(DiagnosticSession.created_at.desc()).limit(100))).scalars().all()
    out = []
    for s in rows:
        env = await db.get(Environment, s.environment_id)
        if s.requested_by == ctx.user.id or ctx.principal.can(P.ACTION_APPROVE, env.name, None):
            out.append(await _session_out(db, s))
    return out


@router.post("/sessions", status_code=201)
async def create(body: SessionIn, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    env = await env_or_404(db, body.environment)
    try:
        sess, plan = await service.request(db, ctx.principal, env, body.image, body.packages, body.ttl_minutes, body.reason)
    except PlanError as exc:
        raise HTTPException(exc.status, exc.message) from None
    return await _session_out(db, sess)


@router.post("/sessions/{sid}/commands", status_code=201)
async def run(sid: uuid.UUID, body: CommandIn, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    s = await _visible(db, ctx, sid)
    env = await db.get(Environment, s.environment_id)
    if not ctx.principal.can(P.ACTION_EXECUTE, env.name, None):
        raise HTTPException(403, f"You need action.execute in {env.name}")
    if s.status != "ready":
        raise HTTPException(409, f"Session is {s.status}")
    try:
        argv = catalog.build_argv(s.image_key, body.tool, body.args)
    except catalog.ArgError as exc:
        await audit.record(db, action="diagnostic.command", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="diagnostic_session",
                           resource_id=str(sid), decision="deny", outcome="denied", details={"tool": body.tool, "args": body.args, "reason": str(exc)})
        raise HTTPException(422, str(exc)) from None
    db.add(DiagnosticCommand(session_id=sid, actor_id=ctx.user.id, tool=body.tool, args=body.args, argv=argv, status="queued"))
    await db.flush()
    return await _session_out(db, s)


@router.get("/sessions/{sid}")
async def get(sid: uuid.UUID, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    return await _session_out(db, await _visible(db, ctx, sid))


@router.post("/sessions/{sid}/terminate")
async def terminate(sid: uuid.UUID, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    s = await _visible(db, ctx, sid)
    if s.status in ("expired", "terminated", "failed"):
        return await _session_out(db, s)
    s.status = "terminated"
    if s.plan_id:
        plan = await db.get(ActionPlan, s.plan_id)
        if plan and plan.status in ("pending", "approved", "queued"):
            plan.status = "cancelled"
    await audit.record(db, action="diagnostic.terminate", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="diagnostic_session", resource_id=str(sid))
    return await _session_out(db, s)
