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
    kind: Literal["minikube", "eks", "kubernetes"]
    is_default: bool = False
    is_production: bool = False
    allowed_namespaces: list[str] = Field(default_factory=lambda: ["*"])
    denied_namespaces: list[str] = Field(default_factory=list)
    max_query_window_minutes: int = Field(default=360, ge=5, le=10080)
    grafana_url: str | None = Field(default=None, max_length=500, pattern=r"^https?://")
    auto_investigate: bool = False
    alert_cooldown_minutes: int = Field(default=30, ge=1, le=1440)
    max_replicas: int = Field(default=10, ge=1, le=1000)
    allow_scale_to_zero: bool = False
    plan_ttl_minutes: int = Field(default=60, ge=5, le=1440)


def env_out(e: Environment) -> dict[str, Any]:
    return {"id": str(e.id), "name": e.name, "display_name": e.display_name or e.name, "kind": e.kind,
            "is_production": e.is_production, "allowed_namespaces": e.allowed_namespaces, "denied_namespaces": e.denied_namespaces,
            "max_query_window_minutes": e.max_query_window_minutes, "grafana_url": e.grafana_url,
            "auto_investigate": e.auto_investigate, "is_default": e.is_default, "alert_cooldown_minutes": e.alert_cooldown_minutes, "max_replicas": e.max_replicas,
            "allow_scale_to_zero": e.allow_scale_to_zero, "plan_ttl_minutes": e.plan_ttl_minutes}


async def sync_automation_binding(db: AsyncSession, env_name: str, enabled: bool) -> None:
    """auto_investigate grants the automation principal investigator on this environment, and
    removing it revokes that binding. Nothing else ever grants the automation principal access."""
    from ..models import AUTOMATION_USER_ID, RoleBinding

    existing = (await db.execute(select(RoleBinding).where(RoleBinding.user_id == AUTOMATION_USER_ID, RoleBinding.environment == env_name,
                                                           RoleBinding.source == "automation"))).scalars().all()
    if enabled and not existing:
        db.add(RoleBinding(user_id=AUTOMATION_USER_ID, role="investigator", environment=env_name, namespace="*", source="automation"))
    if not enabled:
        for b in existing:
            await db.delete(b)


def _validate_patterns(body: EnvIn) -> None:
    import re

    for p in body.allowed_namespaces + body.denied_namespaces:
        if not re.fullmatch(NS_PATTERN, p):
            raise HTTPException(422, f"invalid namespace pattern {p!r}")


EVAL_PREFIX = "eval-"
_HEALTH_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}  # internal fixture environments created by evaluation runs


@router.get("/environments")
async def list_envs(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict[str, Any]]:
    envs = (await db.execute(select(Environment).where(~Environment.name.startswith(EVAL_PREFIX)).order_by(Environment.name))).scalars().all()
    visible = set(ctx.principal.environments_for(P.OVERVIEW_READ, [e.name for e in envs]))
    out = []
    for e in envs:
        if e.name in visible:
            item = env_out(e)
            item["can_investigate"] = bool(ctx.principal.namespace_patterns(P.INVESTIGATION_RUN, e.name))
            out.append(item)
    return out


@router.post("/environments", status_code=201)
async def create_env(body: EnvIn, ctx: AuthContext = Depends(needs(P.ADMIN_POLICIES)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    _validate_patterns(body)
    if body.name.startswith(EVAL_PREFIX):
        raise HTTPException(422, f"names starting with {EVAL_PREFIX!r} are reserved for evaluation runs")
    if (await db.execute(select(Environment).where(Environment.name == body.name))).scalar_one_or_none():
        raise HTTPException(409, "An environment with this name exists")
    e = Environment(**body.model_dump())
    db.add(e)
    await db.flush()
    await sync_automation_binding(db, e.name, e.auto_investigate)
    await audit.record(db, action="environment.create", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="environment",
                       resource_id=e.name, details=body.model_dump())
    return env_out(e)


@router.put("/environments/{name}")
async def update_env(name: str, body: EnvIn, ctx: AuthContext = Depends(needs(P.ADMIN_POLICIES)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    _validate_patterns(body)
    e = (await db.execute(select(Environment).where(Environment.name == name))).scalar_one_or_none()
    if e is None:
        raise HTTPException(404, "Unknown environment")
    before = env_out(e)
    for k, v in body.model_dump(exclude={"name"}).items():
        setattr(e, k, v)
    await sync_automation_binding(db, e.name, e.auto_investigate)
    await audit.record(db, action="environment.update", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="environment",
                       resource_id=name, details={"before": before, "after": body.model_dump()})
    return env_out(e)


@router.get("/overview")
async def overview(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    envs = (await db.execute(select(Environment).where(~Environment.name.startswith(EVAL_PREFIX)).order_by(Environment.name))).scalars().all()
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
                     db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """Bounded live health snapshot: nodes, unhealthy pods in up to 12 permitted namespaces, firing alerts.
    Cached 4 s per user and environment so a 5-second auto-refresh stays cheap; reads are not audited
    individually (one summary per minute would be noise-free; denials/errors still are)."""
    import time as _time

    key = (str(ctx.user.id), environment)
    hit = _HEALTH_CACHE.get(key)
    if hit and hit[0] > _time.monotonic():
        return hit[1]
    ex = await live_executor(request, db, ctx, environment, 60, perm=P.OVERVIEW_READ, audit_success=False)
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
    result["collected_at"] = result["collected_at"] or __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    _HEALTH_CACHE[key] = (_time.monotonic() + 4, result)
    if len(_HEALTH_CACHE) > 2000:
        _HEALTH_CACHE.clear()
    return result
