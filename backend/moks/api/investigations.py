"""Investigations: create, list, read, cancel and stream progress (SSE)."""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..auth.deps import get_auth, require
from ..auth.ratelimit import limiter
from ..auth.sessions import AuthContext, client_ip
from ..config import get_settings
from ..db import get_db, session_scope
from ..models import Environment, Evidence, Investigation, InvestigationEvent, User
from ..rbac import P
from ..scope import ScopeError, effective_scope
from .common import env_or_404, max_window

router = APIRouter(tags=["investigations"])

_WINDOW = re.compile(r"(?i)\b(?:last|past|previous)\s+(\d{1,4})\s*(m|min|mins|minutes?|h|hrs?|hours?|d|days?)\b")
TERMINAL = {"completed", "failed", "cancelled"}


def window_from_question(question: str) -> int | None:
    m = _WINDOW.search(question)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    return n * (1440 if unit.startswith("d") else 60 if unit.startswith("h") else 1)


class CreateIn(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    # Both optional: MOKS resolves them from the question (see resolve_scope).
    environment: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    namespace: str | None = Field(default=None, pattern=r"^(\*|[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)$")
    minutes: int | None = Field(default=None, ge=5, le=10080)
    incident_id: uuid.UUID | None = None


def serialize(inv: Investigation, env_name: str, user_email: str | None = None, full: bool = False) -> dict[str, Any]:
    out = {
        "id": str(inv.id), "question": inv.question, "environment": env_name, "namespace": inv.namespace,
        "status": inv.status, "mode": inv.mode, "created_at": inv.created_at.isoformat() if inv.created_at else None,
        "started_at": inv.started_at.isoformat() if inv.started_at else None,
        "finished_at": inv.finished_at.isoformat() if inv.finished_at else None,
        "time_from": inv.time_from.isoformat(), "time_to": inv.time_to.isoformat(),
        "created_by": user_email, "tool_calls": inv.tool_calls, "tokens_in": inv.tokens_in, "tokens_out": inv.tokens_out,
        "cost_usd": str(inv.cost_usd), "error": inv.error,
        "summary": (inv.report or {}).get("summary") if inv.report else None,
        "incident_id": str(inv.incident_id) if inv.incident_id else None, "trigger": inv.trigger, "stage": inv.stage,
        "group_id": str(inv.group_id) if inv.group_id else None,
    }
    if full:
        out["report"] = inv.report
    return out


@router.get("/investigate/config")
async def investigate_config(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """What an investigation will use: the active chat LLM (or rule-based) and the loaded skills."""
    from .. import skills
    from ..models import LLMConfig

    cfg = (await db.execute(select(LLMConfig).where(LLMConfig.purpose == "chat", LLMConfig.is_active.is_(True)))).scalar_one_or_none()
    envs = (await db.execute(select(Environment).where(~Environment.name.startswith("eval-")).order_by(Environment.name))).scalars().all()
    return {"llm": {"provider": cfg.provider, "model": cfg.model} if cfg else None,
            "skills": sorted(skills.library().skills), "environments": [e.name for e in envs if ctx.principal.namespace_patterns(P.INVESTIGATION_RUN, e.name)]}


async def resolve_scope(db: AsyncSession, ctx: AuthContext, question: str, environment: str | None, namespace: str | None) -> tuple[Environment, str, str]:
    """Environment: the one named, one mentioned in the question, the default one, or the only one
    the user can investigate. Namespace: the one named, one mentioned in the question among the
    user's concrete patterns, otherwise "*" (the worker narrows it from cluster data) when the user
    may read all namespaces, else their single concrete namespace."""
    words = set(re.findall(r"[a-z0-9][a-z0-9-]{1,62}", question.lower()))
    if environment:
        env = await env_or_404(db, environment)
        why_env = "chosen"
    else:
        envs = [e for e in (await db.execute(select(Environment).where(~Environment.name.startswith("eval-")).order_by(Environment.name))).scalars()
                if ctx.principal.namespace_patterns(P.INVESTIGATION_RUN, e.name)]
        if not envs:
            raise HTTPException(403, "You cannot run investigations in any environment")
        named = [e for e in envs if e.name in words or (e.display_name and e.display_name.lower() in question.lower())]
        env = named[0] if named else next((e for e in envs if e.is_default), None) or next((e for e in envs if e.is_production), None) or envs[0]
        why_env = "named in the question" if named else ("default environment" if env.is_default else "only/first environment you can investigate")
    if namespace:
        return env, namespace, why_env
    patterns = ctx.principal.namespace_patterns(P.INVESTIGATION_RUN, env.name)
    concrete = [p for p in patterns if not any(c in p for c in "*?[")]
    hit = [p for p in concrete if p in words]
    if hit:
        return env, hit[0], why_env
    if "*" in patterns:
        return env, "*", why_env
    if len(concrete) == 1:
        return env, concrete[0], why_env
    raise HTTPException(422, f"Mention one of your namespaces ({', '.join(concrete[:10]) or 'none'}) in the question")


class GroupIn(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    environments: list[str] = Field(default_factory=list, max_length=20)  # empty: every environment you may investigate
    minutes: int | None = Field(default=None, ge=1, le=10080)


@router.post("/investigations/group", status_code=201)
async def create_group(body: GroupIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """One question across environments: one investigation each, so evidence and permissions stay
    per environment. Unauthorized environments are refused, not skipped."""
    envs = [e for e in (await db.execute(select(Environment).where(~Environment.name.startswith("eval-")).order_by(Environment.name))).scalars()
            if ctx.principal.namespace_patterns(P.INVESTIGATION_RUN, e.name)]
    names = {e.name for e in envs}
    if body.environments and not set(body.environments) <= names:
        raise HTTPException(403, "you cannot investigate one of those environments")
    chosen = [e for e in envs if not body.environments or e.name in body.environments]
    if not chosen:
        raise HTTPException(403, "You cannot run investigations in any environment")
    group = uuid.uuid4()
    out = []
    for e in chosen[:10]:
        r = await create(CreateIn(question=body.question, environment=e.name, minutes=body.minutes), request, ctx, db)
        (await db.get(Investigation, uuid.UUID(r["id"]))).group_id = group
        out.append(r)
    return {"group_id": str(group), "investigations": out}


class FollowUpIn(BaseModel):
    question: str = Field(min_length=3, max_length=2000)


@router.post("/investigations/{inv_id}/follow-up", status_code=201)
async def follow_up(inv_id: uuid.UUID, body: FollowUpIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    parent, env = await _load_visible(request, db, ctx, inv_id)
    minutes = max(1, int((parent.time_to - parent.time_from).total_seconds() // 60))
    r = await create(CreateIn(question=body.question, environment=env.name, namespace=parent.namespace, minutes=minutes, incident_id=parent.incident_id), request, ctx, db)
    (await db.get(Investigation, uuid.UUID(r["id"]))).parent_id = parent.id
    return r


@router.post("/investigations", status_code=201)
async def create(body: CreateIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    s = get_settings()
    limiter.check(f"inv:{ctx.user.id}", s.rate_investigations_per_minute)
    env, ns, why_env = await resolve_scope(db, ctx, body.question, body.environment, body.namespace)
    body = body.model_copy(update={"environment": env.name, "namespace": ns})
    # "*" (all namespaces) is matched literally: only bindings whose namespace pattern is "*" allow it.
    await require(request, ctx, P.INVESTIGATION_RUN, env.name, body.namespace)
    try:
        scope = effective_scope(ctx.principal, env, P.INVESTIGATION_RUN)
        if body.namespace == "*":
            if not scope.user_patterns:
                raise ScopeError("no namespaces in scope")
        else:
            scope.require(body.namespace)
    except ScopeError as exc:
        from ..auth.deps import deny

        raise await deny(request, ctx, P.INVESTIGATION_RUN, env.name, body.namespace, str(exc))
    minutes = body.minutes or window_from_question(body.question) or s.default_query_window_minutes
    window = min(timedelta(minutes=minutes), max_window(env))
    now = datetime.now(timezone.utc)
    inv = Investigation(created_by=ctx.user.id, environment_id=env.id, namespace=body.namespace, question=body.question,
                        time_from=now - window, time_to=now, status="queued")
    if body.incident_id:
        from ..incidents import add_event
        from ..models import Incident

        inc = await db.get(Incident, body.incident_id)
        if inc is None or inc.environment_id != env.id or not ctx.principal.can(P.INCIDENT_WRITE, env.name, inc.namespace):
            raise HTTPException(404, "Incident not found in this environment")
        inv.incident_id = inc.id
    db.add(inv)
    await db.flush()
    if inv.incident_id:
        await add_event(db, inv.incident_id, "investigation", {"text": f"Investigation started: {body.question[:200]}", "investigation_id": str(inv.id)}, ctx.user.id)
    db.add(InvestigationEvent(investigation_id=inv.id, seq=0, type="queued",
                              payload={"window_minutes": int(window.total_seconds() // 60), "requested_minutes": minutes,
                                       "environment": env.name, "environment_reason": why_env, "namespace": ns}))
    await audit.record(db, action="investigation.create", actor_id=ctx.user.id, actor_email=ctx.user.email,
                       resource_type="investigation", resource_id=str(inv.id), environment=env.name, namespace=body.namespace,
                       decision="allow", details={"question_chars": len(body.question), "window_minutes": int(window.total_seconds() // 60)},
                       ip=client_ip(request))
    await db.flush()
    return serialize(inv, env.name, ctx.user.email)


async def _load_visible(request: Request, db: AsyncSession, ctx: AuthContext, inv_id: uuid.UUID) -> tuple[Investigation, Environment]:
    inv = await db.get(Investigation, inv_id)
    if inv is None:
        raise HTTPException(404, "Investigation not found")
    env = await db.get(Environment, inv.environment_id)
    # An all-namespace investigation may hold evidence from any namespace, so only principals
    # with namespace "*" may read it ("*" is matched literally by fnmatch).
    if not ctx.principal.can(P.INVESTIGATION_READ, env.name, inv.namespace):
        raise HTTPException(404, "Investigation not found")  # do not reveal existence
    return inv, env


@router.get("/investigations")
async def list_(request: Request, environment: str | None = None, limit: int = 50, ctx: AuthContext = Depends(get_auth),
                db: AsyncSession = Depends(get_db, scope="function")) -> list[dict[str, Any]]:
    q = (select(Investigation, Environment.name, User.email).join(Environment, Environment.id == Investigation.environment_id)
         .join(User, User.id == Investigation.created_by).order_by(Investigation.created_at.desc()).limit(min(max(limit, 1), 200) * 3))
    if environment:
        q = q.where(Environment.name == environment)
    out = []
    for inv, env_name, email in (await db.execute(q)).all():
        if ctx.principal.can(P.INVESTIGATION_READ, env_name, inv.namespace):
            out.append(serialize(inv, env_name, email))
        if len(out) >= min(max(limit, 1), 200):
            break
    return out


@router.get("/investigations/{inv_id}")
async def get(inv_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    inv, env = await _load_visible(request, db, ctx, inv_id)
    user = await db.get(User, inv.created_by)
    ev = (await db.execute(select(Evidence).where(Evidence.investigation_id == inv.id).order_by(Evidence.collected_at))).scalars().all()
    out = serialize(inv, env.name, user.email if user else None, full=True)
    out["evidence"] = [{
        "ref": e.ref, "tool": e.tool, "connector_type": e.connector_type, "args": e.args, "scope": e.scope, "ok": e.ok,
        "summary": e.summary, "data": e.data, "source": e.source, "truncated": e.truncated, "error": e.error,
        "duration_ms": e.duration_ms, "collected_at": e.collected_at.isoformat(),
    } for e in sorted(ev, key=lambda e: int(e.ref[1:]))]
    out["grafana_url"] = env.grafana_url
    from ..models import ActionPlan

    plans = (await db.execute(select(ActionPlan).where(ActionPlan.investigation_id == inv.id).order_by(ActionPlan.created_at))).scalars().all()
    out["actions"] = [{"id": str(p.id), "action": p.action, "target": f"{p.target_kind}/{p.target_name}", "status": p.status,
                       "validated": bool((p.result or {}).get("validation", {}).get("ok")), "finished_at": p.finished_at.isoformat() if p.finished_at else None} for p in plans]
    follow = (await db.execute(select(Investigation).where(Investigation.parent_id == inv.id).order_by(Investigation.created_at))).scalars().all()
    out["follow_ups"] = [{"id": str(i.id), "question": i.question, "status": i.status} for i in follow if ctx.principal.can(P.INVESTIGATION_READ, env.name, i.namespace)]
    out["parent_id"] = str(inv.parent_id) if inv.parent_id else None
    return out


@router.post("/investigations/{inv_id}/cancel")
async def cancel(inv_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, str]:
    inv, env = await _load_visible(request, db, ctx, inv_id)
    if inv.created_by != ctx.user.id:
        await require(request, ctx, P.ADMIN_POLICIES, "*", "*")
    if inv.status in TERMINAL:
        return {"status": inv.status}
    inv.cancel_requested = True
    await audit.record(db, action="investigation.cancel", actor_id=ctx.user.id, actor_email=ctx.user.email,
                       resource_type="investigation", resource_id=str(inv.id), environment=env.name, namespace=inv.namespace)
    return {"status": "cancelling"}


@router.get("/investigations/{inv_id}/events")
async def events(inv_id: uuid.UUID, request: Request, after: int = -1, ctx: AuthContext = Depends(get_auth),
                 db: AsyncSession = Depends(get_db, scope="function")) -> StreamingResponse:
    """Server-Sent Events. Resumable with Last-Event-ID; bounded to 10 minutes per connection."""
    await _load_visible(request, db, ctx, inv_id)
    await db.commit()  # release the pooled connection; the stream uses short sessions
    last = request.headers.get("last-event-id")
    cursor = int(last) if last and last.lstrip("-").isdigit() else after

    async def stream():
        nonlocal cursor
        deadline = asyncio.get_running_loop().time() + 600
        idle = 0
        yield "retry: 2000\n\n"
        while asyncio.get_running_loop().time() < deadline:
            if await request.is_disconnected():
                return
            async with session_scope() as s:
                rows = (await s.execute(select(InvestigationEvent).where(
                    InvestigationEvent.investigation_id == inv_id, InvestigationEvent.seq > cursor
                ).order_by(InvestigationEvent.seq).limit(200))).scalars().all()
                status = (await s.get(Investigation, inv_id)).status
            for r in rows:
                cursor = r.seq
                data = json.dumps({"type": r.type, "seq": r.seq, "at": r.created_at.isoformat(), **r.payload}, default=str)
                yield f"id: {r.seq}\nevent: {r.type}\ndata: {data}\n\n"
            if not rows:
                if status in TERMINAL:
                    yield "event: end\ndata: {}\n\n"
                    return
                idle += 1
                if idle % 30 == 0:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.5)
            else:
                idle = 0
        yield "event: timeout\ndata: {}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"})
