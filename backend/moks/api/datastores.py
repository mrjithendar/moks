"""Data page: PostgreSQL and Redis/Valkey clusters an engineer may read, and one view of one cluster at a time.

Every read is a tool call through the ToolExecutor (moks.api.common.live_executor), so the cluster's namespace
check, redaction, truncation and audit are the same as in an investigation. Nothing here writes.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import get_auth
from ..auth.sessions import AuthContext
from ..db import get_db
from .common import live_executor, outcome_response

router = APIRouter(prefix="/datastores", tags=["datastores"])

CLUSTER = r"^[a-z0-9][a-z0-9-]{0,99}$"
VIEWS: dict[str, dict[str, str]] = {
    "postgres": {"overview": "pg_overview", "sessions": "pg_sessions", "locks": "pg_locks", "replication": "pg_replication",
                 "maintenance": "pg_maintenance", "queries": "pg_top_queries"},
    "redis": {"overview": "redis_overview", "slowlog": "redis_slowlog", "clients": "redis_clients", "keyspace": "redis_keyspace"},
}


@router.get("/{environment}")
async def clusters(environment: str, request: Request, health: bool = True, kind: Literal["postgres", "redis"] | None = None,
                   ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    ex = await live_executor(request, db, ctx, environment, None)
    out = await ex.run("datastore_clusters", {"health": health, **({"kind": kind} if kind else {})})
    return {"environment": environment, **outcome_response(out)}


@router.get("/{environment}/{kind}/{cluster}/{view}")
async def cluster_view(environment: str, kind: Literal["postgres", "redis"], cluster: str, view: str, request: Request,
                       min_age_seconds: int | None = Query(default=None, ge=0, le=86400), limit: int | None = Query(default=None, ge=1, le=200),
                       order: str | None = None, sample: int | None = Query(default=None, ge=10, le=2000),
                       ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    tool = VIEWS[kind].get(view)
    if tool is None:
        raise HTTPException(404, f"{kind} clusters have no view {view!r}; use one of {', '.join(VIEWS[kind])}")
    args: dict[str, Any] = {"cluster": cluster}
    extra = {"min_age_seconds": min_age_seconds, "limit": limit, "order": order, "sample": sample}
    from ..tools.builtin import REGISTRY

    fields = REGISTRY[tool].args.model_fields
    args.update({k: v for k, v in extra.items() if v is not None and k in fields})
    ex = await live_executor(request, db, ctx, environment, None)
    return {"environment": environment, "kind": kind, "cluster": cluster, "view": view, **outcome_response(await ex.run(tool, args))}
