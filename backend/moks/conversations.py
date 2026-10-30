"""An investigation conversation: turns from several people, each answered by its own investigation.

Who may do what is decided per action and per person. A turn runs with the permissions of the person
who asked, so joining a conversation never widens anyone's access. Earlier messages reach the model as
untrusted context: one participant's text cannot instruct it on another's behalf.

Delete is soft: the row stays, is hidden from readers and from the model, and can be restored.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .rbac import P, Principal
from .models import Conversation, ConversationMessage, ConversationParticipant, Investigation, User

HISTORY_MESSAGES = 12
HISTORY_CHARS = 1200


async def start(db: AsyncSession, inv: Investigation) -> Conversation:
    conv = Conversation(id=inv.id, title=inv.question[:200], environment_id=inv.environment_id, namespace=inv.namespace, created_by=inv.created_by)
    db.add(conv)
    await db.flush()
    db.add(ConversationParticipant(conversation_id=conv.id, user_id=inv.created_by))
    db.add(ConversationMessage(conversation_id=conv.id, author_id=inv.created_by, role="user", content=inv.question, investigation_id=inv.id))
    inv.conversation_id = conv.id
    await db.flush()
    return conv


async def join(db: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID, added_by: uuid.UUID | None = None) -> bool:
    if await db.get(ConversationParticipant, (conversation_id, user_id)):
        return False
    db.add(ConversationParticipant(conversation_id=conversation_id, user_id=user_id, added_by=added_by))
    return True


async def touch(db: AsyncSession, conversation_id: uuid.UUID) -> None:
    await db.execute(update(Conversation).where(Conversation.id == conversation_id).values(updated_at=datetime.now(timezone.utc)))


async def history(db: AsyncSession, conversation_id: uuid.UUID, before: uuid.UUID, principal: Principal | None = None, environment: str | None = None) -> list[dict[str, str]]:
    """What the model sees of the thread: the latest messages that have not been deleted, and only
    what the person asking may read now. A turn from a scope they have lost is left out."""
    rows = (await db.execute(select(ConversationMessage, User.email, Investigation.namespace)
                             .outerjoin(User, User.id == ConversationMessage.author_id)
                             .outerjoin(Investigation, Investigation.id == ConversationMessage.investigation_id)
                             .where(ConversationMessage.conversation_id == conversation_id, ConversationMessage.deleted_at.is_(None),
                                    ConversationMessage.investigation_id.is_distinct_from(before))
                             .order_by(ConversationMessage.created_at.desc()).limit(HISTORY_MESSAGES))).all()
    readable = [(m, email) for m, email, ns in rows if principal is None or ns is None or principal.can(P.INVESTIGATION_READ, environment, ns)]
    return [{"from": "MOKS" if m.role == "assistant" else (email or "unknown"), "text": m.content[:HISTORY_CHARS]} for m, email in reversed(readable)]


async def record_reply(db: AsyncSession, inv: Investigation, text: str) -> None:
    if inv.conversation_id:
        db.add(ConversationMessage(conversation_id=inv.conversation_id, role="assistant", content=text, investigation_id=inv.id))
        await touch(db, inv.conversation_id)
