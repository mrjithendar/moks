"""The gateway's HTTP and WebSocket surface, at <base>/api/terminal.

A WebSocket carries no CSRF token and browsers send cookies on cross-site upgrades, so the Origin
must match exactly before anything else is read. Only the session's owner can attach.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..auth.deps import get_auth, require
from ..auth.sessions import COOKIE, AuthContext, load
from ..config import get_settings
from ..db import get_db, session_scope
from ..models import Environment, TerminalSession, User
from ..observability import POLICY_DENIALS
from ..rbac import P
from . import service
from .aws import TerminalError
from .profiles import find, kube_namespaces, profiles

log = logging.getLogger("moks.terminal")
router = APIRouter(prefix="/terminal", tags=["terminal"])

UNAUTHENTICATED, FORBIDDEN, NOT_FOUND, TAKEN_OVER, ENDED, BROKEN = 4401, 4403, 4404, 4409, 4410, 4500
WATCH_SECONDS = 5
INPUT_WRITE_SECONDS = 30
_tasks: set[asyncio.Task] = set()


class SessionIn(BaseModel):
    profile: str = Field(max_length=100)
    environment: str = Field(max_length=100)
    account: str = Field(pattern=r"^\d{12}$")
    region: str = Field(max_length=30)
    cluster: str | None = Field(default=None, max_length=200)
    purpose: str = Field(min_length=10, max_length=2000)
    minutes: int = Field(default=60, ge=15, le=720)


def _audit_reader(ctx: AuthContext) -> bool:
    return ctx.principal.can(P.AUDIT_READ, "*", "*")


@router.get("/options")
async def options(ctx: AuthContext = Depends(get_auth)) -> dict:
    """Only what this person may open. The recording policy is stated here so the UI cannot drift from it."""
    s = get_settings()
    rows = [{"name": p.name, "label": p.label, "environment": p.environment, "account": p.account, "role_arn": p.role_arn, "regions": list(p.regions), "clusters": p.clusters()}
            for p in profiles() if ctx.principal.can(P.TERMINAL_USE, p.environment, None)]
    return {"profiles": rows, "idle_minutes": s.terminal_idle_minutes, "max_minutes": s.terminal_max_minutes, "min_minutes": service.MIN_MINUTES, "kube_namespaces": kube_namespaces(),
            "recording": {"input": False, "output": False, "text": "MOKS does not record what is typed or shown in a terminal. It records who opened which scope, when, and how it ended."}}


@router.get("/sessions")
async def sessions(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict]:
    q = select(TerminalSession, User.email).join(User, User.id == TerminalSession.owner_id).order_by(TerminalSession.created_at.desc()).limit(200)
    if not (_audit_reader(ctx) or ctx.principal.can(P.TERMINAL_REVOKE, "*", "*")):
        q = q.where(TerminalSession.owner_id == ctx.user.id)
    return [{**service.session_out(s, email), "mine": s.owner_id == ctx.user.id} for s, email in (await db.execute(q)).all()]


@router.post("/sessions", status_code=201)
async def create(body: SessionIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    await require(request, ctx, P.TERMINAL_USE, body.environment)
    if (await db.execute(select(Environment).where(Environment.name == body.environment))).scalar_one_or_none() is None:
        raise HTTPException(404, f"Unknown environment {body.environment!r}")
    try:
        s = await service.create(db, ctx.user, ctx.session.id, ctx.principal, profile_name=body.profile, environment=body.environment, account=body.account,
                                 region=body.region, cluster=body.cluster, purpose=body.purpose, minutes=body.minutes)
    except TerminalError as exc:
        await audit.record(None, action="terminal.create", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="terminal_session", environment=body.environment,
                           decision="deny", outcome="denied", details={"reason": str(exc), "profile": body.profile, "account": body.account, "region": body.region, "cluster": body.cluster})
        raise HTTPException(422, str(exc)) from None
    out, sid = service.session_out(s, ctx.user.email), s.id
    await db.commit()  # provisioning runs in its own transactions, and must find this row
    task = asyncio.create_task(service.provision(sid, ctx.user.email))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return out


async def _visible(db: AsyncSession, ctx: AuthContext, sid: uuid.UUID) -> TerminalSession:
    s = await db.get(TerminalSession, sid)
    if s is None or not (s.owner_id == ctx.user.id or _audit_reader(ctx) or ctx.principal.can(P.TERMINAL_REVOKE, "*", "*")):
        raise HTTPException(404, "Session not found")
    return s


@router.get("/sessions/{sid}")
async def get(sid: uuid.UUID, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    s = await _visible(db, ctx, sid)
    owner = await db.get(User, s.owner_id)
    return {**service.session_out(s, owner.email if owner else None), "mine": s.owner_id == ctx.user.id}


@router.post("/sessions/{sid}/stop")
async def stop(sid: uuid.UUID, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    s = await _visible(db, ctx, sid)
    mine = s.owner_id == ctx.user.id
    if not mine:
        await require(request, ctx, P.TERMINAL_REVOKE, "*", "*")
    await service.end(db, s, "stopped" if mine else "revoked", ctx.user)
    return {**service.session_out(s), "mine": mine}


@router.get("/sessions/{sid}/cloudtrail")
async def cloudtrail(sid: uuid.UUID, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    s = await _visible(db, ctx, sid)
    profile = find(s.profile)
    if profile is None:
        raise HTTPException(409, f"profile {s.profile} no longer exists")
    try:
        events = await service.aws_factory().trail(profile, s.region, f"moks-audit-{str(s.id)[:8]}", s.role_session_name, service.source_identity(ctx.user.email))
    except TerminalError as exc:
        raise HTTPException(502, str(exc)) from None
    await audit.record(db, action="terminal.cloudtrail", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="terminal_session", resource_id=str(sid),
                       environment=s.environment, details={"events": len(events)})
    return {"events": events, "role_session_name": s.role_session_name, "region": s.region,
            "note": "Management events in this region only, and they can take 15 minutes to appear. Data events need a trail of their own."}


def origin_allowed(origin: str | None) -> bool:
    """Stricter than the HTTP check: a browser always sends Origin on a WebSocket, so none is refused."""
    if not origin:
        return False
    public, got = urlsplit(get_settings().public_url), urlsplit(origin)
    return (got.scheme, got.netloc) == (public.scheme, public.netloc)


async def _deny(ws: WebSocket, code: int, sid: uuid.UUID, reason: str, user: User | None = None) -> None:
    POLICY_DENIALS.labels("terminal").inc()
    await audit.record(None, action="terminal.attach", actor_id=user.id if user else None, actor_email=user.email if user else None, resource_type="terminal_session",
                       resource_id=str(sid), decision="deny", outcome="denied", details={"reason": reason})
    await ws.close(code, reason[:120])


async def _authorize(ws: WebSocket, sid: uuid.UUID) -> tuple[str, uuid.UUID] | None:
    """Everything a new connection must pass. Someone else's session looks like one that does not exist."""
    attachment = uuid.uuid4()
    async with session_scope() as db:
        ctx = await load(db, ws.cookies.get(COOKIE))
        if ctx is None:
            await db.commit()
            await _deny(ws, UNAUTHENTICATED, sid, "not signed in")
            return None
        s = await db.get(TerminalSession, sid)
        if s is None or s.owner_id != ctx.user.id:
            await _deny(ws, NOT_FOUND, sid, "session not found", ctx.user)
            return None
        if not ctx.principal.can(P.TERMINAL_USE, s.environment, None):
            await _deny(ws, FORBIDDEN, sid, f"you need terminal.use in {s.environment}", ctx.user)
            return None
        reason = service.stop_reason(s, service.now())
        if reason:
            await service.end(db, s, reason)
        if s.status != "ready":
            await db.commit()
            await ws.close(ENDED, (service.END_TEXT.get(s.end_reason or "") or f"session is {s.status}")[:120])
            return None
        s.attachment, s.attach_count = attachment, s.attach_count + 1
        await audit.record(db, action="terminal.attach", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="terminal_session", resource_id=str(sid),
                           environment=s.environment, decision="allow", details={"attachment": str(attachment), "count": s.attach_count})
        return s.pod_name, attachment


async def _from_browser(ws: WebSocket, stream, sid: uuid.UUID) -> str:
    last = service.now() - timedelta(seconds=INPUT_WRITE_SECONDS)
    while True:
        try:
            msg = await ws.receive()
        except WebSocketDisconnect:
            return "detached"
        if msg["type"] == "websocket.disconnect":
            return "detached"
        if msg.get("bytes"):  # only keystrokes and paste count as activity; output and pings do not
            await stream.send(msg["bytes"])
            if service.now() - last >= timedelta(seconds=INPUT_WRITE_SECONDS):
                last = service.now()
                await service.mark_input(sid)
        elif msg.get("text"):
            try:
                event = json.loads(msg["text"])
                if event.get("type") == "resize":
                    await stream.resize(max(1, min(int(event["cols"]), 500)), max(1, min(int(event["rows"]), 200)))
            except (ValueError, KeyError, TypeError):
                pass  # the connection's owner sent nonsense; it changes nothing


async def _to_browser(ws: WebSocket, stream) -> str:
    while True:
        data = await stream.receive()
        if data is None:
            return "stream"
        await ws.send_bytes(data)


async def _watch(sid: uuid.UUID, attachment: uuid.UUID) -> str:
    """Ends this connection when the session ends, or when the owner opens it elsewhere."""
    while True:
        await asyncio.sleep(WATCH_SECONDS)
        async with session_scope() as db:
            s = await db.get(TerminalSession, sid)
            if s is None or s.status != "ready":
                return "ended"
            if s.attachment != attachment:
                return "taken"


async def _close_after(ws: WebSocket, outcome: str, sid: uuid.UUID, attachment: uuid.UUID, pods) -> None:
    async with session_scope() as db:
        s = await db.get(TerminalSession, sid)
        if outcome == "stream" and s.status == "ready" and s.attachment == attachment:
            if not await pods.has_shell(s.pod_name):
                await service.end(db, s, "shell_exited")
        if s.status != "ready":
            code, text = ENDED, service.END_TEXT.get(s.end_reason or "") or "The session ended."
        elif s.attachment != attachment:
            code, text = TAKEN_OVER, "This session was opened in another window."
        else:
            code, text = BROKEN, "The connection to the shell dropped."
        await audit.record(db, action="terminal.detach", actor_id=s.owner_id, resource_type="terminal_session", resource_id=str(sid), environment=s.environment,
                           details={"attachment": str(attachment), "outcome": outcome})
    if outcome != "detached":
        try:
            await ws.close(code, text[:120])
        except (RuntimeError, WebSocketDisconnect):
            pass  # the browser left first


@router.websocket("/sessions/{sid}/attach")
async def attach(ws: WebSocket, sid: uuid.UUID) -> None:
    if not origin_allowed(ws.headers.get("origin")):
        POLICY_DENIALS.labels("ws_origin").inc()
        await audit.record(None, action="terminal.attach", resource_type="terminal_session", resource_id=str(sid), decision="deny", outcome="denied",
                           details={"reason": "origin", "origin": (ws.headers.get("origin") or "")[:200]})
        await ws.close(1008)  # before accepting, so the upgrade itself is refused
        return
    await ws.accept()
    got = await _authorize(ws, sid)
    if got is None:
        return
    pod, attachment = got
    pods = service.pods_factory()
    try:
        try:
            stream = await pods.attach(pod)
        except TerminalError as exc:
            await ws.close(BROKEN, str(exc)[:120])
            return
        tasks = [asyncio.create_task(_from_browser(ws, stream, sid)), asyncio.create_task(_to_browser(ws, stream)), asyncio.create_task(_watch(sid, attachment))]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            finished = next(iter(done))
            outcome = finished.result() if not finished.exception() else "stream"
        finally:
            await stream.close()
        await _close_after(ws, outcome, sid, attachment, pods)
    finally:
        await pods.aclose()
