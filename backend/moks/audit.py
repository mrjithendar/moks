"""Append-only audit records.

Rows are hash-chained by a database trigger (migration 0001) and UPDATE/DELETE/TRUNCATE
are rejected. That detects casual tampering but not a database superuser who can drop the
triggers and rewrite the chain; see docs/SECURITY.md for external archival.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .db import session_scope
from .models import AuditEvent
from .redaction import redact_obj

log = logging.getLogger("moks.audit")


async def record(
    db: AsyncSession | None,
    *,
    action: str,
    actor_id: uuid.UUID | str | None = None,
    actor_email: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    environment: str | None = None,
    namespace: str | None = None,
    decision: str = "n/a",
    outcome: str = "success",
    details: dict[str, Any] | None = None,
    ip: str | None = None,
    request_id: str | None = None,
) -> None:
    """Append one audit record. With db=None it is written in its own transaction, so a
    denial is recorded even when the caller's transaction is rolled back."""
    row = AuditEvent(
        actor_id=uuid.UUID(str(actor_id)) if actor_id else None,
        actor_email=actor_email,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        environment=environment,
        namespace=namespace,
        decision=decision,
        outcome=outcome,
        details=redact_obj(details or {}),
        ip=ip,
        request_id=request_id,
    )
    log.info(
        "audit",
        extra={"event": "audit", "action": action, "decision": decision, "outcome": outcome},
    )
    if db is None:
        async with session_scope() as s:
            s.add(row)
    else:
        db.add(row)


async def verify_chain(db: AsyncSession) -> dict[str, Any]:
    """Recompute every hash in chain order. Returns the first broken link, if any."""
    result = await db.execute(
        text(
            """
            SELECT chain_seq, prev_hash, hash,
                   lag(hash) OVER (ORDER BY chain_seq) AS expected_prev,
                   moks_audit_row_hash(prev_hash, chain_seq, a) AS recomputed
            FROM audit_events a ORDER BY chain_seq
            """
        )
    )
    count = 0
    for seq, prev, h, expected_prev, recomputed in result:
        count += 1
        if expected_prev is None:
            expected_prev = "0" * 64
        if prev != expected_prev or h != recomputed:
            return {"ok": False, "checked": count, "broken_at": seq}
    return {"ok": True, "checked": count}
