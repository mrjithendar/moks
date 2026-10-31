"""SRE skills: the published library, its versions and generations, and the policy that governs all use."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, skills
from ..auth.deps import get_auth, needs
from ..auth.sessions import AuthContext
from ..connectors import registry as connectors_registry
from ..db import get_db
from ..models import ComponentVersion, Connector, Environment, SkillGeneration, SkillPolicy, SkillVersion
from ..rbac import P
from ..skills import readiness as rd
from ..skills import registry
from ..versions import FRESH_SECONDS

router = APIRouter(prefix="/skills", tags=["skills"])


def _out(sk: skills.Skill, full: bool = False) -> dict:
    d = {"name": sk.name, "description": sk.meta.description, "source": sk.source, "version": sk.meta.version, "digest": sk.digest, "state": sk.state,
         "triggers": sk.meta.triggers.model_dump(), "tools": sorted({s.tool for s in sk.meta.steps}),
         "remediation": [r.model_dump() for r in sk.meta.remediation], "title": sk.meta.title or sk.name, "owner": sk.meta.owner, "category": sk.meta.category,
         "resource_types": sk.meta.resource_types, "needs": sorted(sk.meta.needs()), "detection_rules": [r.id for r in sk.meta.detection], "environments": sk.meta.environments}
    if full:
        d.update(steps=[s.model_dump(exclude_none=True) for s in sk.meta.steps], body=sk.body, path=sk.path, hypotheses=[h.model_dump() for h in sk.meta.hypotheses],
                 evidence_required=[x if isinstance(x, str) else x.model_dump() for x in sk.meta.evidence_required],
                 verification=[x if isinstance(x, str) else x.model_dump() for x in sk.meta.verification], recovery_validation=sk.meta.recovery_validation,
                 runbooks=sk.meta.runbooks, queries={k: v.model_dump() for k, v in sk.meta.queries.items()}, telemetry=sk.meta.telemetry,
                 detection=[r.model_dump() for r in sk.meta.detection], evaluation=[e.model_dump() for e in sk.meta.evaluation])
    return d


async def _context(db: AsyncSession, ctx: AuthContext, live: bool = False) -> tuple[dict, registry.Policy, dict]:
    """Connectors per readable environment, the policy, and (live) what each environment's Prometheus reports."""
    envs = (await db.execute(select(Environment).where(~Environment.name.startswith("eval-")))).scalars().all()
    names = {e.id: e.name for e in envs if ctx.principal.environments_for(P.INVESTIGATION_READ, [e.name])}
    connectors: dict = {n: set() for n in names.values()}
    for env_id, ctype in (await db.execute(select(Connector.environment_id, Connector.type).where(Connector.enabled.is_(True)))).all():
        if env_id in names:
            connectors[names[env_id]].add(ctype)
    metrics: dict = {}
    if live:
        for env_id, name in names.items():
            rows, _ = await connectors_registry.of_type(db, env_id, "prometheus")
            if rows:
                metrics[name] = await rd.metric_names(str(rows[0][0].id), rows[0][1])
    return connectors, await registry.policy(db), metrics


def _with_state(sk: skills.Skill, connectors: dict, view: registry.Policy, metrics: dict, full: bool = False) -> dict:
    d = _out(sk, full)
    d["enabled"] = sk.name not in view.off
    d["policy_environments"] = view.environments.get(sk.name, [])
    d["availability"] = {}
    for env, have in sorted(connectors.items()):
        allowed, why = registry.allowed(sk, env, view)
        known, complete = metrics.get(env, (None, False))
        state = rd.readiness(sk, have, known, complete)
        if not allowed:
            state = {**state, "state": "unavailable", "available": False, "reasons": [why, *state["reasons"]]}
        d["availability"][env] = state
    return d


@router.get("")
async def list_skills(live: bool = False, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    lib = await registry.refresh()
    connectors, view, metrics = await _context(db, ctx, live)
    return {"skills": [_with_state(s, connectors, view, metrics) for s in sorted(lib.skills.values(), key=lambda s: s.name)], "generation": lib.generation, "digest": lib.digest,
            "readiness_checked": live, "errors": lib.errors if ctx.principal.can(P.ADMIN_POLICIES, "*", "*") else []}


@router.get("/generation")
async def generation(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    """Which generation is active and which one each process is running."""
    from sqlalchemy import text

    active = (await db.execute(select(SkillGeneration).where(SkillGeneration.status == "active").order_by(SkillGeneration.generation.desc()).limit(1))).scalars().first()
    rows = (await db.execute(select(ComponentVersion).where(ComponentVersion.seen_at > text(f"now() - interval '{FRESH_SECONDS} seconds'"), ComponentVersion.component != "executor")
                             .order_by(ComponentVersion.component, ComponentVersion.instance))).scalars().all()
    number = active.generation if active else None
    return {"active": {"generation": number, "digest": active.digest, "reason": active.reason, "created_at": active.created_at.isoformat(), "skills": len(active.manifest)} if active else None,
            "processes": [{"instance": r.instance, "component": r.component, "generation": r.skill_generation, "error": r.skill_error, "current": r.skill_generation == number, "seen_at": r.seen_at.isoformat()} for r in rows],
            "consistent": all(r.skill_generation == number for r in rows)}


@router.get("/registry")
async def versions(ctx: AuthContext = Depends(needs(P.ADMIN_POLICIES)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    rows = (await db.execute(select(SkillVersion).order_by(SkillVersion.skill_id, SkillVersion.created_at.desc()))).scalars().all()
    return {"versions": [{"skill": r.skill_id, "version": r.version, "digest": r.digest, "state": r.state, "source": r.source, "owner": r.owner, "created_at": r.created_at.isoformat(),
                          "published_at": r.published_at.isoformat() if r.published_at else None, "validation": [i for i in r.validation if i["level"] != "info"]} for r in rows if r.state != "published" or r.source != "builtin"],
            "generations": [{"generation": g.generation, "status": g.status, "reason": g.reason, "digest": g.digest, "created_at": g.created_at.isoformat()}
                            for g in (await db.execute(select(SkillGeneration).order_by(SkillGeneration.generation.desc()).limit(20))).scalars()]}


@router.get("/{name}")
async def get_skill(name: str, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    sk = (await registry.refresh()).skills.get(name)
    if sk is None:
        raise HTTPException(404, "Unknown skill")
    connectors, view, metrics = await _context(db, ctx, live=True)
    out = _with_state(sk, connectors, view, metrics, full=True)
    out["history"] = [{"version": r.version, "digest": r.digest, "state": r.state, "published_at": r.published_at.isoformat() if r.published_at else None}
                      for r in (await db.execute(select(SkillVersion).where(SkillVersion.skill_id == name).order_by(SkillVersion.created_at.desc()).limit(20))).scalars()]
    return out


class PolicyIn(BaseModel):
    enabled: bool
    environments: list[str] = Field(default_factory=list, max_length=50)  # empty: every environment
    allow_override: bool = False


@router.put("/{name}/policy")
async def set_policy(name: str, body: PolicyIn, ctx: AuthContext = Depends(needs(P.ADMIN_POLICIES)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    if name not in skills.library().skills:
        raise HTTPException(404, "Unknown skill")
    await db.merge(SkillPolicy(name=name, enabled=body.enabled, environments=body.environments, allow_override=body.allow_override, updated_by=ctx.user.id))
    await audit.record(db, action="skills.policy", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="skill", resource_id=name, decision="allow", details=body.model_dump())
    return {"name": name, **body.model_dump()}


class PublishIn(BaseModel):
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    note: str = Field(default="", max_length=500)


@router.post("/{name}/publish")
async def publish(name: str, body: PublishIn, ctx: AuthContext = Depends(needs(P.ADMIN_POLICIES)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    row = await db.get(SkillVersion, body.digest)
    if row is None or row.skill_id != name:
        raise HTTPException(404, "Unknown skill version")
    try:
        gen = await registry.publish(db, body.digest, ctx.user.id, body.note)
    except registry.PublishError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"generation": gen.generation, "digest": gen.digest}


@router.post("/{name}/rollback")
async def rollback(name: str, body: PublishIn, ctx: AuthContext = Depends(needs(P.ADMIN_POLICIES)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    try:
        gen = await registry.rollback(db, name, body.digest, ctx.user.id, body.note)
    except registry.PublishError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"generation": gen.generation, "digest": gen.digest}


@router.post("/reload")
async def reload(ctx: AuthContext = Depends(needs(P.ADMIN_POLICIES))) -> dict:
    """Registers the files on disk. New built-ins publish; custom versions wait here for an administrator."""
    r = await registry.scan(ctx.user.id)
    await audit.record(None, action="skills.reload", actor_id=ctx.user.id, actor_email=ctx.user.email, details={k: r[k] for k in ("registered", "published", "waiting", "generation")} | {"rejected": len(r["rejected"])})
    return {"loaded": len(skills.library().skills), "errors": r["rejected"], **{k: r[k] for k in ("registered", "published", "waiting", "generation")}}
