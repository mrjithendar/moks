"""SRE skills: list and read the loaded SKILL.md playbooks; admins can reload after a ConfigMap change."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import audit, skills
from ..auth.deps import get_auth, needs
from ..auth.sessions import AuthContext
from ..rbac import P

router = APIRouter(prefix="/skills", tags=["skills"])


def _out(sk: skills.Skill, full: bool = False) -> dict:
    d = {"name": sk.name, "description": sk.meta.description, "source": sk.source, "version": sk.meta.version,
         "triggers": sk.meta.triggers.model_dump(), "tools": sorted({s.tool for s in sk.meta.steps}),
         "remediation": [r.model_dump() for r in sk.meta.remediation]}
    if full:
        d.update(steps=[s.model_dump(exclude_none=True) for s in sk.meta.steps], body=sk.body, path=sk.path)
    return d


@router.get("")
async def list_skills(ctx: AuthContext = Depends(get_auth)) -> dict:
    lib = skills.library()
    return {"skills": [_out(s) for s in sorted(lib.skills.values(), key=lambda s: s.name)],
            "errors": lib.errors if ctx.principal.can(P.ADMIN_POLICIES, "*", "*") else []}


@router.get("/{name}")
async def get_skill(name: str, ctx: AuthContext = Depends(get_auth)) -> dict:
    sk = skills.library().skills.get(name)
    if sk is None:
        raise HTTPException(404, "Unknown skill")
    return _out(sk, full=True)


@router.post("/reload")
async def reload(ctx: AuthContext = Depends(needs(P.ADMIN_POLICIES))) -> dict:
    lib = skills.reload()
    await audit.record(None, action="skills.reload", actor_id=ctx.user.id, actor_email=ctx.user.email,
                       details={"loaded": sorted(lib.skills), "errors": lib.errors})
    return {"loaded": len(lib.skills), "errors": lib.errors}
