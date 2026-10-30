"""Evaluation runs (admin): datasets, start a run (background job), results."""
from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import jobs
from ..auth.deps import needs
from ..auth.sessions import AuthContext
from ..db import get_db
from ..evals import runner
from ..models import EvalRun
from ..rbac import P

router = APIRouter(prefix="/evals", tags=["evals"])


class RunIn(BaseModel):
    dataset: str = "lab"
    mode: Literal["rules", "llm"] = "rules"


@router.get("/datasets")
async def datasets(ctx: AuthContext = Depends(needs(P.ADMIN_POLICIES))) -> list[dict]:
    return runner.list_datasets()


@router.post("/runs", status_code=201)
async def start(body: RunIn, ctx: AuthContext = Depends(needs(P.ADMIN_POLICIES))) -> dict:
    try:
        rid = await runner.start(body.dataset, body.mode, ctx.user.id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    await jobs.enqueue("eval_run", {"run_id": str(rid)}, created_by=ctx.user.id)
    return {"id": str(rid)}


@router.get("/runs")
async def runs(ctx: AuthContext = Depends(needs(P.ADMIN_POLICIES)), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict]:
    rows = (await db.execute(select(EvalRun).order_by(EvalRun.created_at.desc()).limit(50))).scalars().all()
    return [{"id": str(r.id), "dataset": r.dataset, "mode": r.mode, "status": r.status, "score": float(r.score) if r.score is not None else None,
             "results": r.results, "created_at": r.created_at.isoformat(), "finished_at": r.finished_at.isoformat() if r.finished_at else None}
            for r in rows]
