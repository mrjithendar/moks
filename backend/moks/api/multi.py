"""Multi-environment / multi-namespace views (Metrics, Logs, Kubernetes explorer).

GET /multi/{kind}?environment=all|<name>&namespace=all|<name>&<tool args>

Runs the corresponding read-only tool in every environment the user can read (and, for
namespace-bound tools, every permitted namespace), with bounded concurrency, through the same
ToolExecutor (RBAC, scope, bounds, redaction). Successful reads are audited once per request;
denials and errors per call. Partial failures are returned per environment/namespace.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..auth.deps import get_auth
from ..auth.sessions import AuthContext
from ..connectors.base import ConnectorError
from ..db import get_db
from ..models import Environment
from ..rbac import P
from ..tools.builtin import REGISTRY
from .common import live_executor

router = APIRouter(prefix="/multi", tags=["multi"])

KINDS = {
    "pods": "k8s_list_pods", "workloads": "k8s_workloads", "services": "k8s_services", "events": "k8s_events",
    "ingresses": "k8s_ingresses", "nodes": "k8s_nodes", "config-audit": "k8s_config_audit",
    "logs": "loki_logs", "metric-names": "prom_metric_names", "metric": "prom_metric", "alerts": "prom_alerts",
}
NAMESPACE_FANOUT = {"k8s_list_pods", "k8s_workloads", "k8s_services", "k8s_events", "k8s_ingresses", "k8s_config_audit"}
MAX_NAMESPACES = 40
CONCURRENCY = 6


def _coerce(field, raw: str) -> Any:
    ann = str(field.annotation)
    if "bool" in ann:
        return raw.lower() in ("1", "true", "yes")
    if "int" in ann and raw.lstrip("-").isdigit():
        return int(raw)
    if "dict" in ann or "list" in ann:
        import json

        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


@router.get("/scopes")
async def scopes(request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """Environments and namespaces the user can read, for the filter dropdowns."""
    envs = (await db.execute(select(Environment).where(~Environment.name.startswith("eval-")).order_by(Environment.name))).scalars().all()
    display = {e.name: e.display_name for e in envs}
    out = []
    for name in ctx.principal.environments_for(P.EXPLORER_READ, [e.name for e in envs]):
        row: dict[str, Any] = {"environment": name, "display_name": display[name], "namespaces": []}
        try:
            ex = await live_executor(request, db, ctx, name, None, perm=P.EXPLORER_READ, audit_success=False)
            k = ex.ctx.connectors.get("kubernetes")
            row["namespaces"] = ex.ctx.scope.filter(await k[1].list_namespaces()) if k else [n for n in ex.ctx.scope.env_allowed if "*" not in n]
        except (ConnectorError, HTTPException) as exc:
            row["error"] = getattr(exc, "message", None) or str(getattr(exc, "detail", exc))
        out.append(row)
    return {"scopes": out}


class LogSearch(BaseModel):
    environment: str = "all"
    namespace: str = "all"
    minutes: int | None = Field(default=None, ge=1, le=10080)
    pod: str | None = None
    container: str | None = None
    contains: str | None = None
    trace_id: str | None = None
    request_id: str | None = None
    level: str | None = None
    limit: int = Field(default=200, ge=1, le=500)


@router.post("/logs")
async def search_logs(body: LogSearch, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """Search text and IDs stay out of URLs and access logs."""
    filters = {k: v for k, v in body.model_dump(exclude={"environment", "namespace", "minutes"}).items() if v not in (None, "")}
    return await _read("logs", filters, request, body.environment, body.namespace, body.minutes, ctx, db)


@router.get("/{kind}")
async def multi(kind: str, request: Request, environment: str = "all", namespace: str = "all",
                minutes: int | None = Query(default=None, ge=1, le=10080),
                ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    return await _read(kind, dict(request.query_params), request, environment, namespace, minutes, ctx, db)


async def _read(kind: str, params: dict[str, Any], request: Request, environment: str, namespace: str, minutes: int | None, ctx: AuthContext, db: AsyncSession) -> dict[str, Any]:
    tool = KINDS.get(kind)
    if tool is None:
        raise HTTPException(404, f"unknown view {kind!r}")
    spec = REGISTRY[tool]
    args: dict[str, Any] = {}
    for name, field in spec.args.model_fields.items():
        if name in params and name != "namespace":
            args[name] = _coerce(field, str(params[name]))
    if "minutes" in spec.args.model_fields and minutes:
        args["minutes"] = minutes
    envs = (await db.execute(select(Environment).where(~Environment.name.startswith("eval-")).order_by(Environment.name))).scalars().all()
    names = [e.name for e in envs if environment in ("all", e.name)]
    names = ctx.principal.environments_for(P.EXPLORER_READ, names)
    if not names:
        raise HTTPException(403, "no environment you may read")

    items: list[dict] = []
    errors: list[dict] = []
    meta: dict[str, Any] = {"queries": {}, "collected_at": None}
    sem = asyncio.Semaphore(CONCURRENCY)

    async def run_one(ex, env_name: str, ns: str | None) -> None:
        a = dict(args)
        if ns is not None and (spec.namespace_arg or "namespace" in spec.args.model_fields):
            a["namespace"] = ns
        async with sem:
            out = await ex.run(tool, a)
        if not out.ok:
            errors.append({"environment": env_name, "namespace": ns, "error": out.summary, "denied": out.denied})
            return
        meta["collected_at"] = out.collected_at
        if out.source:
            meta["queries"][env_name if ns is None else f"{env_name}/{ns}"] = out.source.get("query") or out.source.get("api")
        data = out.data
        rows: list = []
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = data.get("lines") or data.get("series") or data.get("workloads") or ([{"name": n} for n in data.get("names", [])] if "names" in data else [])
            if kind == "metric":
                meta.setdefault("step", data.get("step"))
            for ref in data.get("missing_references", []):
                meta.setdefault("missing_references", []).append({**ref, "environment": env_name, "namespace": ns})
        for r in rows:
            if isinstance(r, dict):
                r = dict(r)
                r.setdefault("environment", env_name)
                if ns is not None:
                    r.setdefault("namespace", ns)
                if kind == "metric":
                    r["metric"] = {**r.get("metric", {}), "environment": env_name}
                items.append(r)

    tasks = []
    for env_name in names:
        try:
            ex = await live_executor(request, db, ctx, env_name, minutes, perm=P.EXPLORER_READ, audit_success=False)
        except HTTPException as exc:
            errors.append({"environment": env_name, "namespace": None, "error": exc.detail, "denied": True})
            continue
        if tool in NAMESPACE_FANOUT:
            if namespace != "all":
                tasks.append(run_one(ex, env_name, namespace))
                continue
            k = ex.ctx.connectors.get("kubernetes")
            if k is None:
                errors.append({"environment": env_name, "namespace": None, "error": "no kubernetes connector", "denied": False})
                continue
            try:
                nss = ex.ctx.scope.filter(await k[1].list_namespaces())
            except ConnectorError as exc:
                nss = [n for n in ex.ctx.scope.env_allowed if "*" not in n]
                errors.append({"environment": env_name, "namespace": None, "error": exc.message, "denied": False})
            if len(nss) > MAX_NAMESPACES:
                errors.append({"environment": env_name, "namespace": None, "error": f"showing the first {MAX_NAMESPACES} of {len(nss)} namespaces", "denied": False})
            for ns in nss[:MAX_NAMESPACES]:
                tasks.append(run_one(ex, env_name, ns))
        elif tool in ("k8s_nodes", "prom_metric_names", "prom_alerts"):
            tasks.append(run_one(ex, env_name, None))
        else:  # loki_logs / prom_metric: namespace optional in the tool itself
            tasks.append(run_one(ex, env_name, None if namespace == "all" else namespace))
    await asyncio.gather(*tasks)
    if kind == "logs":
        items.sort(key=lambda r: r.get("ts") or "", reverse=True)
        items = items[: int(args.get("limit", 200))]
    if kind == "metric-names":
        merged: dict[str, set] = {}
        for r in items:
            merged.setdefault(r["name"], set()).add(r["environment"])
        items = [{"name": n, "environments": sorted(e)} for n, e in sorted(merged.items())]
    await audit.record(db, action="dashboard.read", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="view", resource_id=kind,
                       environment=None if environment == "all" else environment, namespace=None if namespace == "all" else namespace,
                       decision="allow", details={"tool": tool, "args": args, "environments": names, "items": len(items), "errors": len(errors)})
    return {"items": items, "errors": errors, "environments": names, **meta}
