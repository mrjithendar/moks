"""Action plans: allowlist, create, approval inbox, approve/reject, execute (queue), cancel."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..actions import service
from ..actions.catalog import CATALOG
from ..auth.deps import get_auth
from ..auth.sessions import AuthContext
from ..db import get_db
from ..models import ActionPlan, Approval, Environment, User
from ..rbac import P
from .common import env_or_404

router = APIRouter(prefix="/actions", tags=["actions"])


class PlanIn(BaseModel):
    environment: str
    namespace: str = Field(pattern=r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
    action: str = Field(max_length=40)
    target_kind: str = Field(default="Deployment", max_length=40)
    target_name: str = Field(default="", max_length=253)
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=5, max_length=4000)
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=30)
    incident_id: uuid.UUID | None = None
    investigation_id: uuid.UUID | None = None
    auto_recover: bool = True


class DecisionIn(BaseModel):
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    comment: str = Field(default="", max_length=2000)


def _err(exc: service.PlanError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


async def _out(db: AsyncSession, plan: ActionPlan) -> dict:
    env = await db.get(Environment, plan.environment_id)
    approvals = (await db.execute(select(Approval).where(Approval.plan_id == plan.id).order_by(Approval.created_at))).scalars().all()
    ids = {plan.requested_by, plan.executed_by, *(a.approver_id for a in approvals)} - {None}
    users = {u.id: u.email for u in (await db.execute(select(User).where(User.id.in_(ids)))).scalars()}
    return service.plan_out(plan, env.name, users, [{"approver": users.get(a.approver_id), "decision": a.decision, "comment": a.comment,
                                                       "plan_hash": a.plan_hash, "at": a.created_at.isoformat()} for a in approvals])


@router.get("/catalog")
async def catalog(ctx: AuthContext = Depends(get_auth)) -> list[dict]:
    return [{"action": a.name, "title": a.title, "target_kinds": a.target_kinds, "operation": a.operation,
             "params": a.params.model_json_schema(), "approval_outside_production": a.approval_outside_production} for a in CATALOG.values() if not a.cluster_wide]


@router.post("/plans", status_code=201)
async def create(body: PlanIn, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    env = await env_or_404(db, body.environment)
    try:
        plan = await service.create_plan(db, ctx.principal, env, namespace=body.namespace, action=body.action, target_name=body.target_name,
                                         target_kind=body.target_kind, params=body.params, reason=body.reason, evidence=body.evidence,
                                         incident_id=body.incident_id, investigation_id=body.investigation_id, auto_recover=body.auto_recover)
    except service.PlanError as exc:
        raise _err(exc) from None
    return await _out(db, plan)


@router.get("/plans")
async def list_plans(status: str | None = None, inbox: bool = False, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict]:
    q = select(ActionPlan, Environment.name).join(Environment).order_by(ActionPlan.created_at.desc()).limit(300)
    if status:
        q = q.where(ActionPlan.status == status)
    if inbox:
        q = q.where(ActionPlan.status == "pending", ActionPlan.requires_approval.is_(True), ActionPlan.requested_by != ctx.user.id)
    out = []
    for plan, env_name in (await db.execute(q)).all():
        if not ctx.principal.can(P.INVESTIGATION_READ, env_name, plan.namespace) and not ctx.principal.can(P.ACTION_APPROVE, env_name, plan.namespace):
            continue
        if inbox and not ctx.principal.can(P.ACTION_APPROVE, env_name, plan.namespace):
            continue
        out.append(service.plan_out(plan, env_name))
    return out


@router.get("/plans/{pid}")
async def get(pid: uuid.UUID, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    plan = await db.get(ActionPlan, pid)
    env = await db.get(Environment, plan.environment_id) if plan else None
    if plan is None or not (ctx.principal.can(P.INVESTIGATION_READ, env.name, plan.namespace) or ctx.principal.can(P.ACTION_APPROVE, env.name, plan.namespace)):
        raise HTTPException(404, "Plan not found")
    return await _out(db, plan)


async def _decide(pid, body, ctx, db, approve: bool) -> dict:
    try:
        plan = await service.decide(db, ctx.principal, pid, approve, body.comment, body.plan_hash)
    except service.PlanError as exc:
        await db.commit()  # keep an "expired" transition
        raise _err(exc) from None
    return await _out(db, plan)


@router.post("/plans/{pid}/approve")
async def approve(pid: uuid.UUID, body: DecisionIn, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    return await _decide(pid, body, ctx, db, True)


@router.post("/plans/{pid}/reject")
async def reject(pid: uuid.UUID, body: DecisionIn, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    return await _decide(pid, body, ctx, db, False)


@router.post("/plans/{pid}/execute")
async def execute(pid: uuid.UUID, body: DecisionIn, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    try:
        plan = await service.queue(db, ctx.principal, pid, body.plan_hash)
    except service.PlanError as exc:
        await db.commit()
        raise _err(exc) from None
    return await _out(db, plan)


class ReconcileIn(BaseModel):
    outcome: str
    comment: str = Field(max_length=2000)


@router.post("/plans/{pid}/reconcile")
async def reconcile(pid: uuid.UUID, body: ReconcileIn, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    try:
        plan = await service.reconcile(db, ctx.principal, pid, body.outcome, body.comment)
    except service.PlanError as exc:
        raise _err(exc) from None
    return await _out(db, plan)


@router.post("/plans/{pid}/cancel")
async def cancel(pid: uuid.UUID, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    try:
        plan = await service.cancel(db, ctx.principal, pid)
    except service.PlanError as exc:
        raise _err(exc) from None
    return await _out(db, plan)
