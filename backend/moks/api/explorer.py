"""Interactive live reads (resource explorer, metric charts, log search).

Every endpoint runs through the same ToolExecutor as investigations, so RBAC, environment
scope, query bounds, redaction and audit are identical; results are just not stored as evidence.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import get_auth
from ..auth.sessions import AuthContext
from ..db import get_db
from ..tools.builtin import REGISTRY
from .common import live_executor, outcome_response

router = APIRouter(prefix="/explore/{environment}", tags=["explorer"])

# Tools exposed for interactive use, with their argument names (query parameters).
EXPOSED = {
    "pods": "k8s_list_pods", "pod": "k8s_describe_pod", "logs": "k8s_pod_logs", "events": "k8s_events",
    "workloads": "k8s_workloads", "rollout": "k8s_rollout_history", "services": "k8s_services",
    "ingresses": "k8s_ingresses", "nodes": "k8s_nodes", "dns": "k8s_dns_health", "triage": "k8s_triage",
    "promql": "prom_query", "resources": "prom_pod_resources", "alerts": "prom_alerts", "targets": "prom_targets",
    "log-search": "loki_search", "logql": "loki_query",
}


@router.get("/namespaces")
async def namespaces(environment: str, request: Request, ctx: AuthContext = Depends(get_auth),
                     db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    ex = await live_executor(request, db, ctx, environment, None)
    k = ex.ctx.connectors.get("kubernetes")
    if k is None:
        return {"namespaces": [], "cluster_wide": ex.ctx.scope.cluster_wide, "error": "no kubernetes connector"}
    from ..connectors.base import ConnectorError

    try:
        names = await k[1].list_namespaces()
    except ConnectorError as exc:
        # Without list access, fall back to explicitly allowed namespace names.
        names = [n for n in ex.ctx.scope.env_allowed if "*" not in n]
        return {"namespaces": ex.ctx.scope.filter(names), "cluster_wide": ex.ctx.scope.cluster_wide, "error": exc.message}
    return {"namespaces": ex.ctx.scope.filter(names), "cluster_wide": ex.ctx.scope.cluster_wide}


@router.get("/{kind}")
async def read(environment: str, kind: str, request: Request, minutes: int | None = Query(default=None, ge=1, le=10080),
               ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    tool = EXPOSED.get(kind)
    if tool is None:
        from fastapi import HTTPException

        raise HTTPException(404, f"Unknown explorer view {kind!r}")
    spec = REGISTRY[tool]
    fields = spec.args.model_fields
    args: dict[str, Any] = {}
    for name, field in fields.items():
        if name in request.query_params:
            raw = request.query_params[name]
            ann = field.annotation
            if ann is bool or str(ann).startswith("bool"):
                args[name] = raw.lower() in ("1", "true", "yes")
            elif ann is int or "int" in str(ann):
                try:
                    args[name] = int(raw)
                except ValueError:
                    args[name] = raw
            else:
                args[name] = raw
    if "minutes" in fields and minutes:
        args["minutes"] = minutes
    ex = await live_executor(request, db, ctx, environment, minutes)
    return outcome_response(await ex.run(tool, args))
