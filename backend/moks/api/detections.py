"""Detection rules, policies, dry runs and maintenance windows. Viewing needs investigation.read;
changing policy needs admin.policies."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, detection, skills
from ..auth.deps import get_auth, needs
from ..auth.sessions import AuthContext
from ..db import get_db
from ..models import Connector, DetectionPolicy, DetectionRun, DetectionState, Environment, MaintenanceWindow
from ..rbac import P

router = APIRouter(prefix="/detections", tags=["detections"])


def _rule(rule_id: str) -> detection.Rule:
    rule = next((r for r in detection.all_rules() if r.id == rule_id), None)
    if rule is None:
        raise HTTPException(404, "Unknown rule")
    return rule


async def _readable(db: AsyncSession, ctx: AuthContext) -> list[Environment]:
    envs = (await db.execute(select(Environment).where(~Environment.name.startswith("eval-")).order_by(Environment.name))).scalars().all()
    names = set(ctx.principal.environments_for(P.INVESTIGATION_READ, [e.name for e in envs]))
    return [e for e in envs if e.name in names]


@router.get("/rules")
async def rules(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    envs = await _readable(db, ctx)
    ids = [e.id for e in envs]
    policies = {p.rule_id: p for p in (await db.execute(select(DetectionPolicy))).scalars()}
    runs = (await db.execute(select(DetectionRun).where(DetectionRun.environment_id.in_(ids)))).scalars().all()
    connectors: dict = {}
    for c in (await db.execute(select(Connector).where(Connector.environment_id.in_(ids), Connector.enabled.is_(True)))).scalars():
        connectors.setdefault(c.environment_id, set()).add(c.type)
    names = {e.id: e.name for e in envs}
    out = []
    for r in detection.all_rules():
        policy = policies.get(r.id)
        spec = detection.effective(r, policy)
        needed = detection.RULE_TOOL_CONNECTOR[spec.kind]
        out.append({"id": r.id, "skill": r.skill.name, "version": r.version, "title": spec.title or r.skill.meta.title or r.id, "kind": spec.kind, "severity": spec.severity,
                    "enabled": spec.enabled, "interval_minutes": spec.interval_minutes, "lookback_minutes": spec.lookback_minutes, "sustained_checks": spec.sustained_checks,
                    "recovery_checks": spec.recovery_checks, "cooldown_minutes": spec.cooldown_minutes, "threshold": spec.threshold, "operator": spec.operator, "query": spec.query,
                    "finding": spec.finding, "auto_investigate": spec.auto_investigate, "auto_resolve": spec.auto_resolve, "owner": spec.owner or r.skill.meta.owner,
                    "policy_environments": policy.environments if policy else [],
                    "unavailable_in": [e.name for e in envs if needed not in connectors.get(e.id, set())], "needs": needed,
                    "runs": [{"environment": names[x.environment_id], "at": x.last_run_at.isoformat(), "status": x.last_status, "reason": x.last_reason} for x in runs if x.rule_id == r.id]})
    return {"rules": out, "environments": [{"name": e.name, "detection_enabled": e.detection_enabled} for e in envs]}


class PolicyIn(BaseModel):
    enabled: bool = True
    environments: list[str] = Field(default_factory=list, max_length=50)
    overrides: dict[str, Any] = Field(default_factory=dict)


@router.put("/rules/{rule_id}")
async def set_policy(rule_id: str, body: PolicyIn, ctx: AuthContext = Depends(needs(P.ADMIN_POLICIES)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    rule = _rule(rule_id)
    candidate = DetectionPolicy(rule_id=rule_id, enabled=body.enabled, environments=body.environments, overrides=body.overrides)
    try:
        detection.effective(rule, candidate)
    except ValueError as exc:
        raise HTTPException(422, f"invalid override: {exc}") from None
    await db.merge(DetectionPolicy(rule_id=rule_id, enabled=body.enabled, environments=body.environments, overrides=body.overrides,
                                   updated_by=ctx.user.id, updated_at=datetime.now(timezone.utc)))
    await audit.record(db, action="detection.policy", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="detection_rule", resource_id=rule_id, decision="allow",
                       details=body.model_dump())
    return {"rule": rule_id, **body.model_dump()}


class TestIn(BaseModel):
    environment: str


@router.post("/rules/{rule_id}/test")
async def test_rule(rule_id: str, body: TestIn, ctx: AuthContext = Depends(needs(P.ADMIN_POLICIES)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    rule = _rule(rule_id)
    env = next((e for e in await _readable(db, ctx) if e.name == body.environment), None)
    if env is None:
        raise HTTPException(404, "Environment not found")
    policy = await db.get(DetectionPolicy, rule_id)
    return await detection.dry_run(db, env, rule, policy, ctx.principal)


@router.get("/states")
async def states(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    envs = await _readable(db, ctx)
    names = {e.id: e.name for e in envs}
    rows = (await db.execute(select(DetectionState).where(DetectionState.environment_id.in_(names), DetectionState.status != "cleared")
                             .order_by(DetectionState.last_seen.desc()).limit(200))).scalars().all()
    return {"states": [{"rule": s.rule_id, "environment": names[s.environment_id], "resource": s.resource, "namespace": s.namespace, "status": s.status,
                        "consecutive": s.consecutive, "first_seen": s.first_seen.isoformat(), "last_seen": s.last_seen.isoformat(),
                        "incident_id": str(s.incident_id) if s.incident_id else None} for s in rows
                       if ctx.principal.can(P.INVESTIGATION_READ, names[s.environment_id], s.namespace or "*")]}


class WindowIn(BaseModel):
    environment: str
    namespace: str = "*"
    rule_id: str = "*"
    starts_at: datetime
    ends_at: datetime
    reason: str = Field(min_length=3, max_length=500)


@router.get("/maintenance")
async def windows(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    envs = await _readable(db, ctx)
    names = {e.id: e.name for e in envs}
    rows = (await db.execute(select(MaintenanceWindow).where(MaintenanceWindow.environment_id.in_(names), MaintenanceWindow.ends_at > datetime.now(timezone.utc))
                             .order_by(MaintenanceWindow.starts_at))).scalars().all()
    return {"windows": [{"id": str(w.id), "environment": names[w.environment_id], "namespace": w.namespace, "rule_id": w.rule_id, "starts_at": w.starts_at.isoformat(),
                         "ends_at": w.ends_at.isoformat(), "reason": w.reason} for w in rows]}


@router.post("/maintenance", status_code=201)
async def add_window(body: WindowIn, ctx: AuthContext = Depends(needs(P.ADMIN_POLICIES)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    if body.ends_at <= body.starts_at:
        raise HTTPException(422, "the window must end after it starts")
    env = (await db.execute(select(Environment).where(Environment.name == body.environment))).scalar_one_or_none()
    if env is None:
        raise HTTPException(404, "Environment not found")
    w = MaintenanceWindow(environment_id=env.id, namespace=body.namespace, rule_id=body.rule_id, starts_at=body.starts_at, ends_at=body.ends_at, reason=body.reason, created_by=ctx.user.id)
    db.add(w)
    await db.flush()
    await audit.record(db, action="detection.maintenance", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="maintenance_window", resource_id=str(w.id),
                       environment=env.name, namespace=body.namespace, decision="allow", details={"reason": body.reason, "rule": body.rule_id})
    return {"id": str(w.id)}


@router.delete("/maintenance/{wid}", status_code=204)
async def delete_window(wid: uuid.UUID, ctx: AuthContext = Depends(needs(P.ADMIN_POLICIES)), db: AsyncSession = Depends(get_db, scope="function")) -> None:
    w = await db.get(MaintenanceWindow, wid)
    if w is None:
        raise HTTPException(404, "Window not found")
    await audit.record(db, action="detection.maintenance_delete", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="maintenance_window", resource_id=str(wid), decision="allow")
    await db.delete(w)


def skill_availability(sk: skills.Skill, connectors: set[str]) -> dict:
    return skills.availability(sk, connectors, None)
