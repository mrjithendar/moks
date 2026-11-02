"""Learning from reviewed incidents. An engineer proposes a runbook or skill change from an investigation;
someone else reviews it. Nothing here publishes: approval makes a versioned draft ready for the normal
path, and no log, document or model conclusion becomes permanent knowledge on its own."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..auth.deps import get_auth, needs
from ..auth.sessions import AuthContext
from ..db import get_db
from ..models import Environment, Evidence, ImprovementProposal, Investigation
from ..orchestrator.evidence import LIVE
from ..rbac import P
from ..redaction import redact

router = APIRouter(prefix="/proposals", tags=["proposals"])


class ProposalIn(BaseModel):
    investigation_id: uuid.UUID
    kind: str = Field(pattern="^(runbook|skill)$")
    title: str = Field(min_length=5, max_length=200)
    body: str = Field(min_length=20, max_length=20_000)
    evidence: list[str] = Field(min_length=1, max_length=20)
    owner: str = Field(min_length=2, max_length=200)
    regression_fixture: dict[str, Any]


class ReviewIn(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    note: str = Field(min_length=3, max_length=1000)


def out(p: ImprovementProposal) -> dict[str, Any]:
    return {"id": str(p.id), "investigation_id": str(p.investigation_id), "kind": p.kind, "title": p.title, "body": p.body, "evidence": p.evidence, "owner": p.owner, "version": p.version, "state": p.state,
            "regression_fixture": p.regression_fixture, "created_by": str(p.created_by), "reviewed_by": str(p.reviewed_by) if p.reviewed_by else None, "review_note": p.review_note, "created_at": p.created_at.isoformat() if p.created_at else None}


@router.post("", status_code=201)
async def propose(body: ProposalIn, ctx: AuthContext = Depends(needs(P.INVESTIGATION_RUN)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    inv = await db.get(Investigation, body.investigation_id)
    env = await db.get(Environment, inv.environment_id) if inv else None
    if inv is None or not ctx.principal.can(P.INVESTIGATION_READ, env.name, inv.namespace):
        raise HTTPException(404, "Investigation not found")
    if not body.regression_fixture.get("question"):
        raise HTTPException(422, "a regression fixture needs a question")
    rows = {e.ref: e for e in (await db.execute(select(Evidence).where(Evidence.investigation_id == inv.id, Evidence.ref.in_(body.evidence)))).scalars()}
    bad = [r for r in body.evidence if r not in rows or rows[r].kind not in LIVE]
    if bad:
        raise HTTPException(422, f"these do not cite successful live evidence from this investigation: {', '.join(bad)}")
    version = len((await db.execute(select(ImprovementProposal.id).where(ImprovementProposal.investigation_id == inv.id, ImprovementProposal.title == body.title))).all()) + 1
    p = ImprovementProposal(investigation_id=inv.id, kind=body.kind, title=redact(body.title), body=redact(body.body), evidence=body.evidence, owner=body.owner, version=version,
                            regression_fixture=body.regression_fixture, created_by=ctx.user.id)
    db.add(p)
    await db.flush()
    await audit.record(db, action="proposal.create", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="proposal", resource_id=str(p.id), details={"kind": body.kind, "version": version, "investigation": str(inv.id)})
    return out(p)


@router.get("")
async def list_proposals(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict]:
    rows = (await db.execute(select(ImprovementProposal, Investigation, Environment).join(Investigation, Investigation.id == ImprovementProposal.investigation_id)
                             .join(Environment, Environment.id == Investigation.environment_id).order_by(ImprovementProposal.created_at.desc()).limit(200))).all()
    return [out(p) for p, inv, env in rows if ctx.principal.can(P.INVESTIGATION_READ, env.name, inv.namespace)]


@router.post("/{pid}/review")
async def review(pid: uuid.UUID, body: ReviewIn, ctx: AuthContext = Depends(needs(P.ADMIN_POLICIES)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    p = await db.get(ImprovementProposal, pid, with_for_update=True)
    if p is None:
        raise HTTPException(404, "Proposal not found")
    if p.state != "draft":
        raise HTTPException(409, f"already {p.state}")
    if p.created_by == ctx.user.id:
        raise HTTPException(403, "an author cannot review their own proposal")
    p.state, p.reviewed_by, p.reviewed_at, p.review_note = body.decision, ctx.user.id, datetime.now(timezone.utc), redact(body.note)
    await audit.record(db, action="proposal.review", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="proposal", resource_id=str(pid), decision="allow", details={"decision": body.decision})
    return out(p)
