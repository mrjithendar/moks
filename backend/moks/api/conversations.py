"""Investigation conversations: read, reply, add people, delete and restore.

Reading follows the scope, like investigations. Replying as a note needs read access; asking MOKS needs
investigation.run, and runs as the person asking. Deleting needs the author (messages), the owner
(conversations) or an administrator, and is soft.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, conversations
from ..auth.deps import get_auth
from ..auth.sessions import AuthContext
from ..db import get_db
from ..models import Conversation, ConversationMessage, ConversationParticipant, Environment, Investigation, User
from ..rbac import P
from .investigations import TERMINAL, CreateIn, start_investigation

router = APIRouter(prefix="/conversations", tags=["conversations"])


async def _load(db: AsyncSession, ctx: AuthContext, cid: uuid.UUID, deleted: bool = False) -> tuple[Conversation, Environment]:
    conv = await db.get(Conversation, cid)
    env = await db.get(Environment, conv.environment_id) if conv else None
    if conv is None or not ctx.principal.can(P.INVESTIGATION_READ, env.name, conv.namespace) or (conv.deleted_at is not None and not deleted):
        raise HTTPException(404, "Conversation not found")  # do not reveal existence
    return conv, env


def _is_admin(ctx: AuthContext) -> bool:
    return ctx.principal.can(P.ADMIN_POLICIES, "*", "*")


def _may_manage(ctx: AuthContext, conv: Conversation) -> bool:
    return conv.created_by == ctx.user.id or _is_admin(ctx)


def _message_out(m: ConversationMessage, emails: dict, ctx: AuthContext, conv: Conversation) -> dict[str, Any]:
    gone = m.deleted_at is not None
    may_delete = m.author_id == ctx.user.id or _may_manage(ctx, conv)
    return {"id": str(m.id), "role": m.role, "author": emails.get(m.author_id) if m.author_id else "MOKS", "content": None if gone else m.content, "deleted": gone,
            "deleted_by": emails.get(m.deleted_by) if gone else None, "investigation_id": str(m.investigation_id) if m.investigation_id else None,
            "created_at": m.created_at.isoformat(), "can_delete": may_delete and not gone, "can_restore": may_delete and gone}


async def _detail(db: AsyncSession, ctx: AuthContext, conv: Conversation, env: Environment) -> dict[str, Any]:
    msgs = (await db.execute(select(ConversationMessage).where(ConversationMessage.conversation_id == conv.id).order_by(ConversationMessage.created_at))).scalars().all()
    people = (await db.execute(select(ConversationParticipant).where(ConversationParticipant.conversation_id == conv.id).order_by(ConversationParticipant.joined_at))).scalars().all()
    emails = dict((await db.execute(select(User.id, User.email))).all())
    turns = (await db.execute(select(Investigation.id, Investigation.status).where(Investigation.conversation_id == conv.id))).all()
    return {"id": str(conv.id), "title": conv.title, "environment": env.name, "namespace": conv.namespace, "owner": emails.get(conv.created_by),
            "created_at": conv.created_at.isoformat(), "updated_at": conv.updated_at.isoformat(), "deleted": conv.deleted_at is not None,
            "messages": [_message_out(m, emails, ctx, conv) for m in msgs],
            "participants": [{"email": emails.get(p.user_id), "owner": p.user_id == conv.created_by, "joined_at": p.joined_at.isoformat()} for p in people],
            "running": any(s not in TERMINAL for _, s in turns), "statuses": {str(i): s for i, s in turns},
            "can_ask": ctx.principal.can(P.INVESTIGATION_RUN, env.name, conv.namespace), "can_manage": _may_manage(ctx, conv),
            "is_participant": any(p.user_id == ctx.user.id for p in people)}


@router.get("")
async def list_conversations(deleted: bool = False, limit: int = Query(100, ge=1, le=200), ctx: AuthContext = Depends(get_auth),
                             db: AsyncSession = Depends(get_db, scope="function")) -> list[dict[str, Any]]:
    q = (select(Conversation, Environment.name, User.email).join(Environment, Environment.id == Conversation.environment_id).join(User, User.id == Conversation.created_by)
         .where(Conversation.deleted_at.is_not(None) if deleted else Conversation.deleted_at.is_(None)).order_by(Conversation.updated_at.desc()).limit(limit * 3))
    rows = [(c, e, o) for c, e, o in (await db.execute(q)).all() if ctx.principal.can(P.INVESTIGATION_READ, e, c.namespace) and (not deleted or _may_manage(ctx, c))][:limit]
    ids = [c.id for c, _, _ in rows]
    counts = dict((await db.execute(select(ConversationParticipant.conversation_id, func.count()).where(ConversationParticipant.conversation_id.in_(ids)).group_by(ConversationParticipant.conversation_id))).all())
    msgs = dict((await db.execute(select(ConversationMessage.conversation_id, func.count()).where(ConversationMessage.conversation_id.in_(ids), ConversationMessage.deleted_at.is_(None)).group_by(ConversationMessage.conversation_id))).all())
    running = set((await db.execute(select(Investigation.conversation_id).where(Investigation.conversation_id.in_(ids), Investigation.status.not_in(TERMINAL)))).scalars())
    return [{"id": str(c.id), "title": c.title, "environment": e, "namespace": c.namespace, "owner": o, "updated_at": c.updated_at.isoformat(), "participants": counts.get(c.id, 0),
             "messages": msgs.get(c.id, 0), "running": c.id in running} for c, e, o in rows]


@router.get("/{cid}")
async def get_conversation(cid: uuid.UUID, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    conv, env = await _load(db, ctx, cid, deleted=True)
    if conv.deleted_at is not None and not _may_manage(ctx, conv):
        raise HTTPException(404, "Conversation not found")
    return await _detail(db, ctx, conv, env)


class MessageIn(BaseModel):
    content: str = Field(min_length=3, max_length=2000)
    ask: bool = True  # False: a note to the other people, with no investigation
    minutes: int | None = Field(default=None, ge=5, le=10080)


@router.post("/{cid}/messages", status_code=201)
async def post_message(cid: uuid.UUID, body: MessageIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    conv, env = await _load(db, ctx, cid)
    can_run = ctx.principal.can(P.INVESTIGATION_RUN, env.name, conv.namespace)
    if body.ask and not can_run:
        raise HTTPException(403, "Asking MOKS needs investigation.run here; you can still post a note if you are in this conversation")
    if not body.ask and not can_run and await db.get(ConversationParticipant, (conv.id, ctx.user.id)) is None:
        raise HTTPException(403, "Ask someone in this conversation to add you first")
    await conversations.join(db, conv.id, ctx.user.id)
    if body.ask:
        created = await start_investigation(CreateIn(question=body.content, environment=env.name, namespace=conv.namespace, minutes=body.minutes), request, ctx, db, conv.id)
        await audit.record(db, action="conversation.turn", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="conversation", resource_id=str(conv.id),
                           environment=env.name, namespace=conv.namespace, decision="allow", details={"investigation_id": created["id"]})
    else:
        db.add(ConversationMessage(conversation_id=conv.id, author_id=ctx.user.id, role="user", content=body.content))
        await conversations.touch(db, conv.id)
        await audit.record(db, action="conversation.note", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="conversation", resource_id=str(conv.id),
                           environment=env.name, namespace=conv.namespace, decision="allow", details={"chars": len(body.content)})
    await db.flush()
    return await _detail(db, ctx, conv, env)


class ParticipantIn(BaseModel):
    email: str = Field(max_length=320)


@router.post("/{cid}/participants", status_code=201)
async def add_participant(cid: uuid.UUID, body: ParticipantIn, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    from ..actions.service import principal_of

    conv, env = await _load(db, ctx, cid)
    if await db.get(ConversationParticipant, (conv.id, ctx.user.id)) is None and not _is_admin(ctx):
        raise HTTPException(403, "Only people in this conversation can add others")
    user = (await db.execute(select(User).where(User.email == body.email.strip().lower()))).scalar_one_or_none()
    readable = False
    if user is not None and user.is_active:
        readable = (await principal_of(db, user.id)).can(P.INVESTIGATION_READ, env.name, conv.namespace)
    if not readable:  # one answer, so this cannot be used to find out who has an account
        raise HTTPException(422, f"That person cannot be added: they need read access to {env.name}/{conv.namespace}")
    await conversations.join(db, conv.id, user.id, ctx.user.id)
    await audit.record(db, action="conversation.participant_add", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="conversation", resource_id=str(conv.id),
                       environment=env.name, namespace=conv.namespace, decision="allow", details={"added": user.email})
    await db.flush()
    return await _detail(db, ctx, conv, env)


async def _stop_running(db: AsyncSession, conversation_id: uuid.UUID, investigation_id: uuid.UUID | None = None) -> None:
    q = select(Investigation).where(Investigation.conversation_id == conversation_id, Investigation.status.not_in(TERMINAL))
    if investigation_id:
        q = q.where(Investigation.id == investigation_id)
    for inv in (await db.execute(q)).scalars():
        inv.cancel_requested = True


@router.delete("/{cid}")
async def delete_conversation(cid: uuid.UUID, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, str]:
    conv, env = await _load(db, ctx, cid)
    if not _may_manage(ctx, conv):
        raise HTTPException(403, "Only the person who started this conversation or an administrator can delete it")
    conv.deleted_at, conv.deleted_by = datetime.now(timezone.utc), ctx.user.id
    await _stop_running(db, conv.id)
    await audit.record(db, action="conversation.delete", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="conversation", resource_id=str(conv.id),
                       environment=env.name, namespace=conv.namespace, decision="allow")
    return {"status": "deleted"}


@router.post("/{cid}/restore")
async def restore_conversation(cid: uuid.UUID, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    conv, env = await _load(db, ctx, cid, deleted=True)
    if not _may_manage(ctx, conv):
        raise HTTPException(404, "Conversation not found")
    conv.deleted_at, conv.deleted_by = None, None
    await audit.record(db, action="conversation.restore", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="conversation", resource_id=str(conv.id),
                       environment=env.name, namespace=conv.namespace, decision="allow")
    return await _detail(db, ctx, conv, env)


async def _message(db: AsyncSession, ctx: AuthContext, cid: uuid.UUID, mid: uuid.UUID) -> tuple[Conversation, Environment, ConversationMessage]:
    conv, env = await _load(db, ctx, cid)
    msg = await db.get(ConversationMessage, mid)
    if msg is None or msg.conversation_id != conv.id:
        raise HTTPException(404, "Message not found")
    if msg.author_id != ctx.user.id and not _may_manage(ctx, conv):
        raise HTTPException(403, "Only its author, the conversation owner or an administrator can change this message")
    return conv, env, msg


@router.delete("/{cid}/messages/{mid}")
async def delete_message(cid: uuid.UUID, mid: uuid.UUID, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    conv, env, msg = await _message(db, ctx, cid, mid)
    msg.deleted_at, msg.deleted_by = datetime.now(timezone.utc), ctx.user.id
    if msg.investigation_id and msg.role == "user":
        await _stop_running(db, conv.id, msg.investigation_id)
    await audit.record(db, action="conversation.message_delete", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="conversation_message", resource_id=str(msg.id),
                       environment=env.name, namespace=conv.namespace, decision="allow", details={"conversation": str(conv.id), "role": msg.role})
    await db.flush()
    return await _detail(db, ctx, conv, env)


@router.post("/{cid}/messages/{mid}/restore")
async def restore_message(cid: uuid.UUID, mid: uuid.UUID, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    conv, env, msg = await _message(db, ctx, cid, mid)
    msg.deleted_at, msg.deleted_by = None, None
    await audit.record(db, action="conversation.message_restore", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="conversation_message", resource_id=str(msg.id),
                       environment=env.name, namespace=conv.namespace, decision="allow", details={"conversation": str(conv.id)})
    await db.flush()
    return await _detail(db, ctx, conv, env)


