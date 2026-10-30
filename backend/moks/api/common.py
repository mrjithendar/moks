from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import deny
from ..auth.sessions import AuthContext
from ..config import get_settings
from ..connectors import registry
from ..models import Environment
from ..rbac import P
from ..scope import ScopeError, effective_scope
from ..tools.contracts import ToolContext
from ..tools.executor import ToolExecutor


async def env_or_404(db: AsyncSession, name: str) -> Environment:
    env = (await db.execute(select(Environment).where(Environment.name == name))).scalar_one_or_none()
    if env is None:
        raise HTTPException(404, f"Unknown environment {name!r}")
    return env


def max_window(env: Environment) -> timedelta:
    return timedelta(minutes=min(env.max_query_window_minutes, get_settings().max_query_window_minutes))


async def live_executor(request: Request, db: AsyncSession, ctx: AuthContext, env_name: str, minutes: int | None,
                        perm: P = P.EXPLORER_READ) -> ToolExecutor:
    """Executor for interactive (non-investigation) reads: same policy checks, no evidence rows."""
    env = await env_or_404(db, env_name)
    try:
        scope = effective_scope(ctx.principal, env, perm)
    except ScopeError as exc:
        raise await deny(request, ctx, perm, env_name, None, str(exc))
    connectors, errors = await registry.for_environment(db, env.id)
    now = datetime.now(timezone.utc)
    window = min(timedelta(minutes=minutes or get_settings().default_query_window_minutes), max_window(env))
    tctx = ToolContext(None, env, scope, ctx.principal, now - window, now, connectors, max_window(env))
    ex = ToolExecutor(db, tctx, timeout_seconds=30, read_permissions=(perm,))
    ex.connector_errors = errors  # type: ignore[attr-defined]
    return ex


def outcome_response(out) -> dict[str, Any]:
    if out.denied:
        raise HTTPException(403, out.error)
    if not out.ok:
        status = {"invalid_args": 422, "not_found": 404, "config": 409, "timeout": 504}.get(out.error_kind or "", 502)
        raise HTTPException(status, out.summary)
    return {"summary": out.summary, "data": out.data, "source": out.source, "truncated": out.truncated,
            "collected_at": out.collected_at, "duration_ms": out.duration_ms}
