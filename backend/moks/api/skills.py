"""SRE skills: the published library, its versions and generations, and the policy that governs all use."""
from __future__ import annotations

import difflib

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, skills
from ..auth.deps import get_auth, needs
from ..auth.sessions import AuthContext
from ..connectors import registry as connectors_registry
from ..db import get_db
from ..models import ComponentVersion, Connector, Environment, EvalRun, Investigation, SkillGeneration, SkillPolicy, SkillVersion
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
                 verification=[x if isinstance(x, str) else x.model_dump() for x in sk.meta.verification], recovery_validation=[x if isinstance(x, str) else x.model_dump() for x in sk.meta.recovery_validation],
                 runbooks=sk.meta.runbooks, queries={k: v.model_dump() for k, v in sk.meta.queries.items()}, telemetry=sk.meta.telemetry, permissions=sk.meta.permissions, requires=sk.meta.requires,
                 inputs=[i.model_dump() for i in sk.meta.inputs], outputs=[o.model_dump() for o in sk.meta.outputs], preconditions=[c.model_dump() for c in sk.meta.preconditions], budget=sk.meta.budget.model_dump(),
                 detection=[r.model_dump() for r in sk.meta.detection], evaluation=[e.model_dump() for e in sk.meta.evaluation], enforced=enforcement(sk))
    return d


def enforcement(sk: skills.Skill) -> list[dict]:
    """Each part of the contract and whether MOKS checks it. The page shows this, so guidance never reads as a guarantee."""
    out = [{"field": "steps", "item": s.id or s.tool, "enforced": True, "how": "run through the policy-checked executor; " + ("a skip is a gap" if s.required else "a skip is recorded")} for s in sk.meta.steps]
    for c in sk.meta.preconditions:
        out.append({"field": "preconditions", "item": c.label or f"{c.finding} {c.expect}", "enforced": True, "how": "checked before the first step"})
    for i in sk.meta.inputs:
        out.append({"field": "inputs", "item": i.name, "enforced": i.required, "how": "a missing required input stops the skill" if i.required else "optional"})
    for o in sk.meta.outputs:
        out.append({"field": "outputs", "item": o.name, "enforced": False, "how": "describes what to expect"})
    for x in sk.meta.evidence_required:
        out.append({"field": "evidence_required", "item": x, "enforced": False, "how": "guidance only"} if isinstance(x, str) else {"field": "evidence_required", "item": x.label or x.tool, "enforced": not x.optional, "how": "optional: reported only" if x.optional else f"at least {x.min_ok} successful calls, or the conclusion is not verified"})
    for x in sk.meta.verification:
        out.append({"field": "verification", "item": x, "enforced": False, "how": "guidance only"} if isinstance(x, str) else {"field": "verification", "item": x.label or x.finding, "enforced": True, "how": f"finding must be {x.expect}"})
    for x in sk.meta.recovery_validation:
        out.append({"field": "recovery_validation", "item": x if isinstance(x, str) else x.label or x.finding, "enforced": False, "how": "guidance only" if isinstance(x, str) else "passed to the proposed action; the action plan's own validation is what runs"})
    for h in sk.meta.hypotheses:
        out.append({"field": "hypotheses", "item": h.statement, "enforced": bool(h.supported_by or h.contradicted_by), "how": "reported as supported, contradicted or untested; never a conclusion" if h.supported_by or h.contradicted_by else "guidance only"})
    for k, v in sk.meta.budget.model_dump(exclude_none=True).items():
        out.append({"field": "budget", "item": f"{k} {v}", "enforced": True, "how": "checked between steps"})
    return out


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
            "readiness_checked": live, "errors": lib.errors if ctx.principal.can(P.SKILL_REVIEW) else []}


@router.get("/generation")
async def generation(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    """Which generation is active and which one each process is running."""
    from sqlalchemy import text

    active = (await db.execute(select(SkillGeneration).where(SkillGeneration.status == "active").order_by(SkillGeneration.generation.desc()).limit(1))).scalars().first()
    rows = (await db.execute(select(ComponentVersion).where(ComponentVersion.seen_at > text(f"now() - interval '{FRESH_SECONDS} seconds'"), ComponentVersion.component != "executor")
                             .order_by(ComponentVersion.component, ComponentVersion.instance))).scalars().all()
    number = active.generation if active else None
    return {"active": {"generation": number, "digest": active.digest, "reason": active.reason, "created_at": active.created_at.isoformat(), "skills": len(active.manifest)} if active else None,
            "processes": [{"instance": r.instance, "component": r.component, "generation": r.skill_generation, "error": r.skill_error, "current": r.skill_generation == number, "compatible": not r.skill_error, "seen_at": r.seen_at.isoformat()} for r in rows],
            "consistent": all(r.skill_generation == number for r in rows)}


@router.get("/registry")
async def versions(ctx: AuthContext = Depends(needs(P.SKILL_REVIEW)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    rows = (await db.execute(select(SkillVersion).order_by(SkillVersion.skill_id, SkillVersion.created_at.desc()))).scalars().all()
    return {"versions": [{"skill": r.skill_id, "version": r.version, "digest": r.digest, "state": r.state, "source": r.source, "owner": r.owner, "created_at": r.created_at.isoformat(), "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
                          "review_note": r.review_note, "published_at": r.published_at.isoformat() if r.published_at else None, "validation": [i for i in r.validation if i["level"] != "info"]} for r in rows if r.state != "published" or r.source != "builtin"],
            "generations": [{"generation": g.generation, "status": g.status, "reason": g.reason, "digest": g.digest, "created_at": g.created_at.isoformat(), "previous": g.previous, "skills": len(g.manifest)}
                            for g in (await db.execute(select(SkillGeneration).order_by(SkillGeneration.generation.desc()).limit(20))).scalars()]}


@router.get("/generations/{number}/diff")
async def generation_diff(number: int, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    gen = await db.get(SkillGeneration, number)
    if gen is None:
        raise HTTPException(404, "Unknown generation")
    before = (await db.get(SkillGeneration, gen.previous)).manifest if gen.previous else {}
    changed = sorted(n for n in gen.manifest.keys() & before.keys() if gen.manifest[n] != before[n])
    names = {r.digest: r.version for r in (await db.execute(select(SkillVersion).where(SkillVersion.digest.in_([*gen.manifest.values(), *before.values()])))).scalars()}
    return {"generation": number, "previous": gen.previous, "reason": gen.reason, "added": sorted(gen.manifest.keys() - before.keys()), "removed": sorted(before.keys() - gen.manifest.keys()),
            "changed": [{"skill": n, "from": names.get(before[n]), "to": names.get(gen.manifest[n]), "from_digest": before[n], "to_digest": gen.manifest[n]} for n in changed]}


@router.get("/{name}")
async def get_skill(name: str, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    sk = (await registry.refresh()).skills.get(name)
    if sk is None:
        raise HTTPException(404, "Unknown skill")
    connectors, view, metrics = await _context(db, ctx, live=True)
    out = _with_state(sk, connectors, view, metrics, full=True)
    can_review = ctx.principal.can(P.SKILL_REVIEW)
    out["history"] = [{"version": r.version, "digest": r.digest, "state": r.state, "source": r.source, "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None, "published_at": r.published_at.isoformat() if r.published_at else None}
                      for r in (await db.execute(select(SkillVersion).where(SkillVersion.skill_id == name).order_by(SkillVersion.created_at.desc()).limit(20))).scalars() if can_review or r.state in ("published", "deprecated")]
    run = (await db.execute(select(EvalRun).where(EvalRun.kind == "skills", EvalRun.status == "completed").order_by(EvalRun.created_at.desc()).limit(1))).scalars().first()
    out["evaluation_results"] = {"run": str(run.id), "created_at": run.created_at.isoformat(), "generation": (run.versions or {}).get("skills_generation"), "cases": ((run.versions or {}).get("metrics") or {}).get("by_skill", {}).get(name, [])} if run else None
    return out


async def _version(db: AsyncSession, name: str, digest: str, ctx: AuthContext) -> SkillVersion:
    """Unpublished text is for reviewers. To anyone else it does not exist."""
    row = await db.get(SkillVersion, digest)
    if row is None or row.skill_id != name or (row.state not in ("published", "deprecated") and not ctx.principal.can(P.SKILL_REVIEW)):
        raise HTTPException(404, "Unknown skill version")
    return row


@router.get("/{name}/versions/{digest}")
async def version(name: str, digest: str, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    """One version as a reader would see it, whether or not it runs anywhere."""
    row = await _version(db, name, digest, ctx)
    sk = skills.parse(row.content, f"registry:{name}@{row.version}", row.source)
    sk.state = row.state
    return {**_out(sk, full=True), "validation": row.validation, "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None, "review_note": row.review_note, "published_at": row.published_at.isoformat() if row.published_at else None}


@router.get("/{name}/diff")
async def diff(name: str, base: str, head: str, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    a, b = await _version(db, name, base, ctx), await _version(db, name, head, ctx)
    return {"from": a.version, "to": b.version, "from_digest": a.digest, "to_digest": b.digest,
            "lines": list(difflib.unified_diff(a.content.splitlines(), b.content.splitlines(), f"{name} v{a.version}", f"{name} v{b.version}", lineterm="", n=3))[:2000]}


@router.get("/{name}/runs")
async def runs(name: str, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict]:
    """Execution traces of investigations this person may read: each step, skipped or run, and why."""
    rows = (await db.execute(select(Investigation, Environment).join(Environment, Environment.id == Investigation.environment_id).where(Investigation.skill_pins.contains([{"skill": name}]))
                             .order_by(Investigation.created_at.desc()).limit(100))).all()
    out = []
    for inv, env in rows:
        if ctx.principal.can(P.INVESTIGATION_READ, env.name, inv.namespace) and len(out) < 20:
            run = next((r for r in inv.skill_pins if r.get("skill") == name), {})
            out.append({"investigation": str(inv.id), "environment": env.name, "namespace": inv.namespace, "created_at": inv.created_at.isoformat(), "generation": inv.skill_generation, "version": run.get("version"), "digest": run.get("digest"),
                        "outcome": run.get("outcome"), "not_run": run.get("not_run"), "stopped": run.get("stopped"), "escalation": run.get("escalation"), "reasons": run.get("reasons"), "readiness": run.get("readiness"), "steps": run.get("steps", []),
                        "checks": run.get("checks", []), "hypotheses": run.get("hypotheses", []), "gaps": run.get("gaps", []), "budget_exhausted": run.get("budget_exhausted", False)})
    return out


class PolicyIn(BaseModel):
    """What is left out stays as it was: switching a skill off must not also forget its environment limits or override decision."""
    enabled: bool | None = None
    environments: list[str] | None = Field(default=None, max_length=50)  # empty: every environment
    allow_override: bool | None = None


@router.put("/{name}/policy")
async def set_policy(name: str, body: PolicyIn, ctx: AuthContext = Depends(needs(P.SKILL_PUBLISH)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    known = name in skills.library().skills or (await db.execute(select(SkillVersion.digest).where(SkillVersion.skill_id == name).limit(1))).first() is not None
    if not known:
        raise HTTPException(404, "Unknown skill")
    row = await db.get(SkillPolicy, name) or SkillPolicy(name=name, enabled=True, environments=[], allow_override=False)
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    for key, value in changes.items():
        setattr(row, key, value)
    row.updated_by = ctx.user.id
    db.add(row)
    await audit.record(db, action="skills.policy", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="skill", resource_id=name, decision="allow", details=changes)
    return {"name": name, "enabled": row.enabled, "environments": row.environments, "allow_override": row.allow_override}


class PublishIn(BaseModel):
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    note: str = Field(default="", max_length=500)


class ReviewIn(BaseModel):
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    note: str = Field(min_length=3, max_length=500)


@router.post("/{name}/validate")
async def validate_version(name: str, body: PublishIn, ctx: AuthContext = Depends(needs(P.SKILL_REVIEW)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    """The whole generation this would produce, checked as publication will check it. Changes nothing."""
    row = await db.get(SkillVersion, body.digest)
    if row is None or row.skill_id != name:
        raise HTTPException(404, "Unknown skill version")
    try:
        return await registry.preview(db, body.digest)
    except registry.PublishError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/{name}/review")
async def review(name: str, body: ReviewIn, ctx: AuthContext = Depends(needs(P.SKILL_REVIEW)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    row = await db.get(SkillVersion, body.digest)
    if row is None or row.skill_id != name:
        raise HTTPException(404, "Unknown skill version")
    try:
        row = await registry.review(db, body.digest, ctx.user.id, body.note)
    except registry.PublishError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"digest": row.digest, "reviewed_at": row.reviewed_at.isoformat()}


@router.post("/{name}/publish")
async def publish(name: str, body: PublishIn, ctx: AuthContext = Depends(needs(P.SKILL_PUBLISH)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    row = await db.get(SkillVersion, body.digest)
    if row is None or row.skill_id != name:
        raise HTTPException(404, "Unknown skill version")
    try:
        gen = await registry.publish(db, body.digest, ctx.user.id, body.note)
    except registry.PublishError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"generation": gen.generation, "digest": gen.digest}


@router.post("/{name}/rollback")
async def rollback(name: str, body: PublishIn, ctx: AuthContext = Depends(needs(P.SKILL_PUBLISH)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    try:
        gen = await registry.rollback(db, name, body.digest, ctx.user.id, body.note)
    except registry.PublishError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"generation": gen.generation, "digest": gen.digest}


@router.post("/reload")
async def reload(ctx: AuthContext = Depends(needs(P.SKILL_PUBLISH))) -> dict:
    """Registers the files on disk. Everything waits here for an explicit publication."""
    r = await registry.register_all()
    await audit.record(None, action="skills.reload", actor_id=ctx.user.id, actor_email=ctx.user.email, details={k: r[k] for k in ("registered", "waiting")})
    return {"loaded": len(skills.library().skills), "errors": r["rejected"], "registered": r["registered"], "published": [], "waiting": r["waiting"], "generation": registry.snapshot()["generation"]}
