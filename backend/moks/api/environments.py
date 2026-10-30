"""Environments (with infrastructure scope) and the overview page."""
from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..auth.deps import get_auth, needs
from ..auth.sessions import AuthContext
from ..db import get_db
from ..models import Connector, Environment, Investigation
from ..rbac import P
from .common import live_executor

router = APIRouter(tags=["environments"])

NS_PATTERN = r"^[a-z0-9*?\-]{1,63}$"


class EnvIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    display_name: str = Field(default="", max_length=200)
    kind: Literal["minikube", "eks"]
    is_production: bool = False
    allowed_namespaces: list[str] = Field(default_factory=lambda: ["*"])
    denied_namespaces: list[str] = Field(default_factory=list)
    max_query_window_minutes: int = Field(default=360, ge=5, le=10080)
    grafana_url: str | None = Field(default=None, max_length=500, pattern=r"^https?://")


def env_out(e: Environment) -> dict[str, Any]:
    return {"id": str(e.id), "name": e.name, "display_name": e.display_name or e.name, "kind": e.kind,
            "is_production": e.is_production, "allowed_namespaces": e.allowed_namespaces, "denied_namespaces": e.denied_namespaces,
            "max_query_window_minutes": e.max_query_window_minutes, "grafana_url": e.grafana_url}


def _validate_patterns(body: EnvIn) -> None:
    import re

    for p in body.allowed_namespaces + body.denied_namespaces:
        if not re.fullmatch(NS_PATTERN, p):
            raise HTTPException(422, f"invalid namespace pattern {p!r}")


@router.get("/environments")
async def list_envs(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    envs = (await db.execute(select(Environment).order_by(Environment.name))).scalars().all()
    visible = set(ctx.principal.environments_for(P.OVERVIEW_READ, [e.name for e in envs]))
    out = []
    for e in envs:
        if e.name in visible:
            item = env_out(e)
            item["can_investigate"] = bool(ctx.principal.namespace_patterns(P.INVESTIGATION_RUN, e.name))
            out.append(item)
    return out


@router.post("/environments", status_code=201)
async def create_env(body: EnvIn, ctx: AuthContext = Depends(needs(P.ADMIN_POLICIES)), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    _validate_patterns(body)
    if (await db.execute(select(Environment).where(Environment.name == body.name))).scalar_one_or_none():
        raise HTTPException(409, "An environment with this name exists")
    e = Environment(**body.model_dump())
    db.add(e)
    await db.flush()
    await audit.record(db, action="environment.create", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="environment",
                       resource_id=e.name, details=body.model_dump())
    return env_out(e)


@router.put("/environments/{name}")
async def update_env(name: str, body: EnvIn, ctx: AuthContext = Depends(needs(P.ADMIN_POLICIES)), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    _validate_patterns(body)
    e = (await db.execute(select(Environment).where(Environment.name == name))).scalar_one_or_none()
    if e is None:
        raise HTTPException(404, "Unknown environment")
    before = env_out(e)
    for k, v in body.model_dump(exclude={"name"}).items():
        setattr(e, k, v)
    await audit.record(db, action="environment.update", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="environment",
                       resource_id=name, details={"before": before, "after": body.model_dump()})
    return env_out(e)


@router.get("/overview")
async def overview(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    envs = (await db.execute(select(Environment).order_by(Environment.name))).scalars().all()
    names = set(ctx.principal.environments_for(P.OVERVIEW_READ, [e.name for e in envs]))
    out = []
    for e in envs:
        if e.name not in names:
            continue
        conns = (await db.execute(select(Connector).where(Connector.environment_id == e.id).order_by(Connector.type))).scalars().all()
        active = (await db.execute(select(func.count()).select_from(Investigation).where(
            Investigation.environment_id == e.id, Investigation.status.in_(["queued", "running"])))).scalar_one()
        out.append({**env_out(e), "active_investigations": active, "connectors": [{
            "id": str(c.id), "type": c.type, "name": c.name, "enabled": c.enabled, "last_test_ok": c.last_test_ok,
            "last_test_at": c.last_test_at.isoformat() if c.last_test_at else None, "last_test_message": c.last_test_message,
        } for c in conns]})
    return {"environments": out}


@router.get("/overview/{environment}/health")
async def env_health(environment: str, request: Request, ctx: AuthContext = Depends(get_auth),
                     db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Bounded live health snapshot: nodes, unhealthy pods in up to 12 permitted namespaces, firing alerts."""
    ex = await live_executor(request, db, ctx, environment, 60, perm=P.OVERVIEW_READ)
    result: dict[str, Any] = {"collected_at": None, "nodes": None, "unhealthy_pods": [], "alerts": None, "errors": {}}
    result["errors"].update(getattr(ex, "connector_errors", {}))
    k = ex.ctx.connectors.get("kubernetes")
    if k:
        from ..connectors.base import ConnectorError

        try:
            nss = ex.ctx.scope.filter(await k[1].list_namespaces())[:12]
            if ex.ctx.scope.cluster_wide:
                nodes = await ex.run("k8s_nodes", {})
                result["nodes"] = nodes.data if nodes.ok else None
                if not nodes.ok:
                    result["errors"]["nodes"] = nodes.summary
            outs = await asyncio.gather(*[ex.run("k8s_list_pods", {"namespace": ns, "unhealthy_only": True}) for ns in nss])
            for o in outs:
                if o.ok:
                    result["unhealthy_pods"].extend(o.data)
                    result["collected_at"] = o.collected_at
                else:
                    result["errors"][o.args.get("namespace", "k8s")] = o.summary
            result["namespaces_checked"] = nss
        except ConnectorError as exc:
            result["errors"]["kubernetes"] = exc.message
    if ex.ctx.connectors.get("prometheus"):
        a = await ex.run("prom_alerts", {"firing_only": True})
        result["alerts"] = a.data if a.ok else None
        if not a.ok:
            result["errors"]["prometheus"] = a.summary
    return result
