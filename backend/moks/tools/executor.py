"""The single gate between investigations and live systems.

For every call it: resolves the tool (unknown -> denied), refuses non-read-only/privileged
tools (those go through action plans), validates arguments, re-checks application RBAC and
infrastructure scope, enforces the time window and a per-call timeout, redacts and truncates
the result, stores it as citable evidence and writes an audit record. Denials are recorded
the same way. None of these checks depend on who asked: the UI, the API or the LLM planner.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..config import get_settings
from ..connectors.base import ConnectorError
from ..models import Evidence
from ..observability import POLICY_DENIALS, TOOL_CALLS, tracer
from ..rbac import P
from ..redaction import redact_obj
from ..scope import ScopeError
from .builtin import REGISTRY
from .contracts import ToolContext, ToolSpec


@dataclass
class ToolOutcome:
    ok: bool
    tool: str
    args: dict[str, Any]
    summary: str
    data: Any = None
    source: dict[str, Any] | None = None
    error: str | None = None
    error_kind: str | None = None
    denied: bool = False
    truncated: bool = False
    evidence_ref: str | None = None
    duration_ms: int = 0
    collected_at: str | None = None


log = logging.getLogger("moks.tools")


def truncate_json(data: Any, max_bytes: int) -> tuple[Any, bool]:
    """Shrink data to roughly max_bytes of JSON, keeping structure. Returns (data, truncated)."""
    def size(x: Any) -> int:
        return len(json.dumps(x, default=str))

    if size(data) <= max_bytes:
        return data, False
    if isinstance(data, list):
        lo, hi = 0, len(data)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if size(data[:mid]) <= max_bytes:
                lo = mid
            else:
                hi = mid - 1
        if lo == 0 and data:
            first, _ = truncate_json(data[0], max_bytes)
            return [first], True
        return data[:lo], True
    if isinstance(data, dict):
        out = dict(data)
        for _ in range(40):
            if size(out) <= max_bytes:
                return out, True
            key = max(out, key=lambda k: size(out[k]))
            budget = max(200, size(out[key]) // 2)
            out[key], _ = truncate_json(out[key], budget)
            if isinstance(out[key], str) and len(out[key]) < 250:
                out[key] = out[key][:200] + "…[truncated]"
        return out, True
    if isinstance(data, str):
        return data[: max(0, max_bytes - 20)] + "…[truncated]", True
    return data, False


def tools_for_planner(ctx: ToolContext) -> list[ToolSpec]:
    """Tools the LLM may choose from: read-only, non-privileged, connector configured, and
    allowed for this principal's scope."""
    out = []
    for spec in REGISTRY.values():
        if not spec.read_only or spec.privileged:
            continue
        if not all(ctx.available(c) for c in (spec.connector_type, *spec.also_requires)):
            continue
        if spec.cluster_scoped and not ctx.scope.cluster_wide:
            continue
        if spec.extra_permission and spec.namespace_arg is None and not ctx.principal.can(spec.extra_permission, ctx.environment.name, "*"):
            continue
        if spec.extra_permission and spec.namespace_arg and not ctx.principal.can_anywhere(spec.extra_permission):
            continue
        out.append(spec)
    return out


class ToolExecutor:
    def __init__(self, db: AsyncSession | None, ctx: ToolContext, *, next_ref=None, timeout_seconds: float = 30.0,
                 read_permissions: tuple[P, ...] = (P.EXPLORER_READ, P.INVESTIGATION_RUN), audit_success: bool = True):
        self.db = db
        # Dashboards (overview refresh, multi-environment views) audit one summary per request
        # instead of every successful read; denials and errors are always audited.
        self.audit_success = audit_success
        self.read_permissions = read_permissions
        self.ctx = ctx
        self.settings = get_settings()
        self.timeout = timeout_seconds
        self._next_ref = next_ref

    async def _deny(self, tool: str, args: dict, reason: str, kind: str) -> ToolOutcome:
        POLICY_DENIALS.labels(kind).inc()
        TOOL_CALLS.labels(tool if tool in REGISTRY else "unknown", "denied").inc()
        await audit.record(
            None, action="tool.call", actor_id=self.ctx.principal.user_id, actor_email=self.ctx.principal.email,
            resource_type="tool", resource_id=tool[:200], environment=self.ctx.environment.name,
            namespace=str(args.get("namespace"))[:253] if isinstance(args, dict) and args.get("namespace") else None,
            decision="deny", outcome="denied",
            details={"reason": reason, "kind": kind, "args": args, "investigation_id": str(self.ctx.investigation_id) if self.ctx.investigation_id else None},
        )
        return ToolOutcome(False, tool, args, f"denied: {reason}", error=reason, error_kind=kind, denied=True)

    def _authorize(self, spec: ToolSpec, parsed) -> None:
        env = self.ctx.environment.name
        if spec.namespace_arg:
            ns = getattr(parsed, spec.namespace_arg)
            if not any(self.ctx.principal.can(perm, env, ns) for perm in self.read_permissions):
                raise ScopeError(f"you have no read access to namespace {ns!r} in {env!r}")
            self.ctx.scope.require(ns)
        if spec.cluster_scoped:
            self.ctx.scope.require_cluster_wide(spec.name)
        if spec.extra_permission:
            ns = getattr(parsed, spec.namespace_arg) if spec.namespace_arg else "*"
            if not self.ctx.principal.can(spec.extra_permission, env, ns):
                where = f"namespace {ns!r}" if spec.namespace_arg else "all namespaces"
                raise ScopeError(f"{spec.name} requires {spec.extra_permission} for {where} in {env!r}")

    async def run(self, tool: str, args: dict[str, Any] | None) -> ToolOutcome:
        args = args if isinstance(args, dict) else {}
        if self.ctx.guard is not None:
            await self.ctx.guard.check()  # raises AccessRevoked, which ends the whole investigation
        spec = REGISTRY.get(tool)
        if spec is None:
            return await self._deny(tool, args, f"unknown tool {tool!r}", "unknown_tool")
        if not spec.read_only or spec.privileged:
            return await self._deny(tool, args, "mutating and privileged tools require an approved action plan", "privileged_tool")
        try:
            parsed = spec.args.model_validate(args)
        except ValidationError as exc:
            msg = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
            TOOL_CALLS.labels(tool, "invalid_args").inc()
            return ToolOutcome(False, tool, args, f"invalid arguments: {msg}", error=msg, error_kind="invalid_args")
        clean_args = parsed.model_dump(exclude_none=True)
        try:
            self._authorize(spec, parsed)
        except ScopeError as exc:
            return await self._deny(tool, clean_args, str(exc), "scope")

        start = time.monotonic()
        started_at = time.time()
        with tracer().start_as_current_span(f"tool {tool}") as span:
            span.set_attribute("moks.tool", tool)
            span.set_attribute("moks.environment", self.ctx.environment.name)
            try:
                result = await asyncio.wait_for(spec.handler(self.ctx, parsed), timeout=self.timeout)
                outcome = ToolOutcome(True, tool, clean_args, result.summary, data=result.data, source=result.source)
            except ScopeError as exc:  # raised by handlers that check scope on query content
                return await self._deny(tool, clean_args, str(exc), "scope")
            except ConnectorError as exc:
                outcome = ToolOutcome(False, tool, clean_args, f"{spec.connector_type} error: {exc.message}", error=exc.message, error_kind=exc.kind)
            except asyncio.TimeoutError:
                outcome = ToolOutcome(False, tool, clean_args, f"timed out after {self.timeout:.0f}s", error="timeout", error_kind="timeout")
            except Exception as exc:  # defensive: a tool bug must not kill the investigation
                log.exception("tool failed", extra={"event": "tool_failed", "tool": tool})
                outcome = ToolOutcome(False, tool, clean_args, f"internal error in {tool} ({type(exc).__name__})", error=type(exc).__name__, error_kind="internal")
            span.set_attribute("moks.outcome", "ok" if outcome.ok else (outcome.error_kind or "error"))
        outcome.duration_ms = int((time.monotonic() - start) * 1000)
        outcome.collected_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at))
        if outcome.ok:
            outcome.data, outcome.truncated = truncate_json(redact_obj(outcome.data), self.settings.max_result_bytes)
        TOOL_CALLS.labels(tool, "ok" if outcome.ok else (outcome.error_kind or "error")).inc()
        log.info("tool call", extra={"event": "tool_call", "tool": tool, "operation": tool, "dependency": spec.connector_type, "outcome": "success" if outcome.ok else "failure",
                                     "durationMs": outcome.duration_ms, **({} if outcome.ok else {"errorCode": outcome.error_kind or "error"})})

        if self.db is not None and self._next_ref is not None:
            ref = self._next_ref()
            outcome.evidence_ref = ref
            entry = self.ctx.connectors.get(spec.connector_type)
            self.db.add(Evidence(
                investigation_id=self.ctx.investigation_id, ref=ref, tool=tool,
                connector_id=entry[0].id if entry and entry[0] is not None else None, connector_type=spec.connector_type,
                args=clean_args,
                scope={"environment": self.ctx.environment.name, "namespace": clean_args.get("namespace"),
                       "from": self.ctx.time_from.isoformat(), "to": self.ctx.time_to.isoformat()},
                ok=outcome.ok, summary=outcome.summary, data=outcome.data, source=outcome.source or {},
                truncated=outcome.truncated, error=outcome.error, duration_ms=outcome.duration_ms,
            ))
        if outcome.ok and not self.audit_success:
            return outcome
        # Dashboards fan out with gather() over one session, which allows a single operation at a time.
        lock = self.db.info.setdefault("moks_audit_lock", asyncio.Lock()) if self.db is not None else None
        async with lock or contextlib.nullcontext():
            await audit.record(
                self.db, action="tool.call", actor_id=self.ctx.principal.user_id, actor_email=self.ctx.principal.email,
                resource_type="tool", resource_id=tool, environment=self.ctx.environment.name,
                namespace=clean_args.get("namespace"), decision="allow", outcome="success" if outcome.ok else "error",
                details={"args": clean_args, "evidence": outcome.evidence_ref, "error_kind": outcome.error_kind,
                         "duration_ms": outcome.duration_ms, "investigation_id": str(self.ctx.investigation_id) if self.ctx.investigation_id else None},
            )
        return outcome
