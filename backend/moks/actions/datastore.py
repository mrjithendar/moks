"""Approval-gated actions on PostgreSQL and Redis clusters: cancel a query, end a session, close a client.

Two credentials, two processes. The API plans with the cluster's read-only monitoring connector: it reads the one
session or client the plan is about, and the server's identity. Only the executor builds an *operator* connector,
from config.executor (the operator role or ACL user) and config.executor_credential_ref (a credential mounted only
into the executor). Without both, the connector has no write target and nothing can be planned (service.write_target).

What makes it safe to act later on what was reviewed earlier:
- PostgreSQL: the signal statement itself matches pid AND backend_start AND backend_type, so a reused pid is never
  signalled. The executor also checks the server's system identifier, so a plan cannot land on another cluster.
- Redis: client IDs are never reused while the server runs, and run_id changes when it restarts, so the executor
  checks run_id and then the client's address before CLIENT KILL ID.

A signal is sent once. If its outcome cannot be established, the plan goes to reconciliation_required.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..connectors import registry
from ..connectors.base import ConnectorError
from ..db import session_scope
from ..models import Connector, Environment

KINDS = {"Postgres": "postgres", "Redis": "redis"}
POLL = 0.5


def _row_namespace(row: Connector) -> str | None:
    ns = (row.config or {}).get("namespace")
    return str(ns) if ns else None


async def find(db: AsyncSession, env: Environment, target_kind: str, name: str) -> tuple[Connector, Any]:
    rows, errors = await registry.of_type(db, env.id, KINDS[target_kind])
    for row, inst in rows:
        if row.name == name:
            return row, inst
    if name in errors:
        raise ConnectorError("config", f"{target_kind} cluster {name!r} is misconfigured: {errors[name]}")
    raise ConnectorError("not_found", f"no enabled {target_kind} cluster named {name!r} in {env.name}")


async def preflight(db: AsyncSession, env: Environment, action: str, target_kind: str, name: str, namespace: str, params: Any) -> tuple[Connector, dict[str, Any]]:
    """Plan time, with the read-only connector: the reviewed session or client, and the server identity it lives on.
    The plan's namespace must be the cluster's own, so a grant in one namespace cannot reach a cluster in another."""
    row, inst = await find(db, env, target_kind, name)
    own = _row_namespace(row)
    if own is None:
        raise ConnectorError("forbidden", f"{name} has no namespace configured, so MOKS cannot tell who may act on it; set config.namespace")
    if own != namespace:
        raise ConnectorError("forbidden", f"{name} belongs to namespace {own}, not {namespace}")
    if target_kind == "Postgres":
        session = await inst.session(params.pid)
        if session is None:
            raise ConnectorError("not_found", f"no session with pid {params.pid} on {name} (it may have ended)")
        keep = ("pid", "backend_start", "backend_type", "superuser", "user", "database", "application", "client", "state", "xact_age_s", "query")
        pre = {k: session.get(k) for k in keep}
        pre["server"] = await inst.server_identity()
    else:
        client = await inst.client(params.client_id)
        if client is None:
            raise ConnectorError("not_found", f"no client with id {params.client_id} on {name} (it may have disconnected)")
        pre = {"client_id": client.get("id"), "addr": client.get("addr"), "name": client.get("name"), "cmd": client.get("cmd"),
               "idle": client.get("idle"), "age": client.get("age"), "server": await inst.server_identity()}
    return row, pre


def operator_connector(row: Connector) -> Any:
    """Executor only. The operator identity replaces the monitoring one; every other setting (host, TLS, timeouts) is kept."""
    from ..connectors.postgres import PostgresConnector
    from ..connectors.redis import RedisConnector

    cfg = dict(row.config or {})
    op = cfg.get("executor") or {}
    cred = cfg.get("executor_credential_ref")
    if not isinstance(op, dict) or not op.get("username") or not cred:
        raise ConnectorError("config", f"connector {row.name} needs config.executor.username and config.executor_credential_ref to act")
    cfg["username"] = op["username"]
    if row.type == "postgres":
        return PostgresConnector(cfg, cred)
    return RedisConnector(cfg, cred, operator=True)


async def execute(fence: Any, snap: dict[str, Any], identity: dict[str, Any] | None, timeout: int, result: dict[str, Any]) -> str:
    action, name, pre, params = snap["action"], snap["target_name"], snap["pre_state"] or {}, snap["params"]
    if not identity or not identity.get("connector_id"):
        raise ConnectorError("config", "this plan has no write target")
    async with session_scope() as db:
        row = await db.get(Connector, uuid.UUID(identity["connector_id"]))
    if row is None or not row.enabled:
        raise ConnectorError("config", f"the connector for {name} no longer exists or is disabled")
    if _row_namespace(row) != snap["namespace"]:
        raise ConnectorError("conflict", f"{name} no longer belongs to namespace {snap['namespace']}")
    op = operator_connector(row)
    try:
        if await op.server_identity() != pre.get("server"):
            raise ConnectorError("conflict", f"{name} is not the server this plan was reviewed against (it was replaced or restarted)")
        if row.type == "postgres":
            return await _postgres(fence, op, action, params, timeout, result)
        return await _redis(fence, op, params, timeout, result)
    finally:
        await op.aclose()


async def _postgres(fence: Any, pg: Any, action: str, params: dict, timeout: int, result: dict) -> str:
    pid, started = int(params["pid"]), str(params["backend_start"])
    before = await pg.session(pid)
    if before is None or str(before.get("backend_start")) != started:
        raise ConnectorError("conflict", f"session {pid} has ended or its pid now belongs to another session; nothing was signalled")
    kind = "cancel" if action == "pg_cancel_query" else "terminate"
    await fence.checkpoint("intent", pid=pid, backend_start=started, signal=kind)
    signalled = await pg.signal(kind, pid, started)  # one attempt: an unknown outcome goes to reconciliation
    await fence.checkpoint("applied" if signalled else "not_applied", signalled=signalled)
    result["steps"].append({"step": "execute", "signal": kind, "pid": pid, "signalled": signalled})
    if not signalled:
        result["validation"] = {"ok": False, "reason": "PostgreSQL did not signal the session: it ended, or it is no longer the reviewed one"}
        return "failed"
    deadline = time.monotonic() + timeout
    while True:
        now = await pg.session(pid)
        same = now is not None and str(now.get("backend_start")) == started
        if kind == "terminate" and not same:
            result["validation"] = {"ok": True, "gone": True}
            return "succeeded"
        if kind == "cancel" and (not same or now.get("state") != "active" or now.get("query_start") != before.get("query_start")):
            result["validation"] = {"ok": True, "state": now.get("state") if same else "ended"}
            return "succeeded"
        if time.monotonic() > deadline:
            result["validation"] = {"ok": False, "reason": f"the session was still {now.get('state') if now else 'present'} after {timeout}s"}
            return "failed"
        await asyncio.sleep(POLL)


async def _redis(fence: Any, r: Any, params: dict, timeout: int, result: dict) -> str:
    cid, addr = int(params["client_id"]), params["addr"]
    client = await r.client(cid)
    if client is None:
        raise ConnectorError("conflict", f"client {cid} has already disconnected; nothing was closed")
    if client.get("addr") != addr:
        raise ConnectorError("conflict", f"client {cid} now has address {client.get('addr')}, not the reviewed {addr}")
    await fence.checkpoint("intent", client_id=cid)
    closed = await r.kill_client(cid)
    await fence.checkpoint("applied" if closed else "not_applied", closed=closed)
    result["steps"].append({"step": "execute", "client_id": cid, "closed": closed})
    deadline = time.monotonic() + timeout
    while True:
        if await r.client(cid) is None:
            result["validation"] = {"ok": True, "gone": True}
            return "succeeded" if closed else "failed"
        if time.monotonic() > deadline:
            result["validation"] = {"ok": False, "reason": f"client {cid} was still connected after {timeout}s"}
            return "failed"
        await asyncio.sleep(POLL)
