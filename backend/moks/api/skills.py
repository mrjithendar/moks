"""SRE skills: list and read the loaded SKILL.md playbooks; admins can reload after a ConfigMap change."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, skills
from ..auth.deps import get_auth, needs
from ..auth.sessions import AuthContext
from ..db import get_db
from ..models import Connector, Environment, SkillPolicy
from ..rbac import P

router = APIRouter(prefix="/skills", tags=["skills"])


def _out(sk: skills.Skill, full: bool = False) -> dict:
    d = {"name": sk.name, "description": sk.meta.description, "source": sk.source, "version": sk.meta.version,
         "triggers": sk.meta.triggers.model_dump(), "tools": sorted({s.tool for s in sk.meta.steps}),
         "remediation": [r.model_dump() for r in sk.meta.remediation], "title": sk.meta.title or sk.name, "owner": sk.meta.owner, "category": sk.meta.category,
         "resource_types": sk.meta.resource_types, "needs": sorted(sk.meta.needs()), "detection_rules": [r.id for r in sk.meta.detection]}
    if full:
        d.update(steps=[s.model_dump(exclude_none=True) for s in sk.meta.steps], body=sk.body, path=sk.path, hypotheses=[h.model_dump() for h in sk.meta.hypotheses],
                 evidence_required=sk.meta.evidence_required, verification=sk.meta.verification, recovery_validation=sk.meta.recovery_validation,
                 runbooks=sk.meta.runbooks, queries={k: v.model_dump() for k, v in sk.meta.queries.items()}, telemetry=sk.meta.telemetry,
                 detection=[r.model_dump() for r in sk.meta.detection], evaluation=[e.model_dump() for e in sk.meta.evaluation])
    return d


async def _context(db: AsyncSession, ctx: AuthContext) -> tuple[dict, set]:
    """Connectors per readable environment, and the skills an administrator switched off."""
    envs = (await db.execute(select(Environment).where(~Environment.name.startswith("eval-")))).scalars().all()
    names = {e.id: e.name for e in envs if ctx.principal.environments_for(P.INVESTIGATION_READ, [e.name])}
    connectors: dict = {n: set() for n in names.values()}
    for env_id, ctype in (await db.execute(select(Connector.environment_id, Connector.type).where(Connector.enabled.is_(True)))).all():
        if env_id in names:
            connectors[names[env_id]].add(ctype)
    off = set((await db.execute(select(SkillPolicy.name).where(SkillPolicy.enabled.is_(False)))).scalars())
    return connectors, off


def _with_state(sk: skills.Skill, connectors: dict, off: set, full: bool = False) -> dict:
    d = _out(sk, full)
    d["enabled"] = sk.name not in off
    d["availability"] = {env: skills.availability(sk, have, None) for env, have in sorted(connectors.items())}
    return d


@router.get("")
async def list_skills(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    lib = skills.library()
    connectors, off = await _context(db, ctx)
    return {"skills": [_with_state(s, connectors, off) for s in sorted(lib.skills.values(), key=lambda s: s.name)],
            "errors": lib.errors if ctx.principal.can(P.ADMIN_POLICIES, "*", "*") else []}


@router.get("/{name}")
async def get_skill(name: str, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    sk = skills.library().skills.get(name)
    if sk is None:
        raise HTTPException(404, "Unknown skill")
    connectors, off = await _context(db, ctx)
    return _with_state(sk, connectors, off, full=True)


class PolicyIn(BaseModel):
    enabled: bool


@router.put("/{name}/policy")
async def set_policy(name: str, body: PolicyIn, ctx: AuthContext = Depends(needs(P.ADMIN_POLICIES)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    if name not in skills.library().skills:
        raise HTTPException(404, "Unknown skill")
    await db.merge(SkillPolicy(name=name, enabled=body.enabled, updated_by=ctx.user.id))
    await audit.record(db, action="skills.policy", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="skill", resource_id=name, decision="allow", details=body.model_dump())
    return {"name": name, "enabled": body.enabled}


@router.post("/reload")
async def reload(ctx: AuthContext = Depends(needs(P.ADMIN_POLICIES))) -> dict:
    lib = skills.reload()
    await audit.record(None, action="skills.reload", actor_id=ctx.user.id, actor_email=ctx.user.email,
                       details={"loaded": sorted(lib.skills), "errors": lib.errors})
    return {"loaded": len(lib.skills), "errors": lib.errors}
