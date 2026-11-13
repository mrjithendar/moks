"""Read-only tools over PostgreSQL and Redis/Valkey clusters (connector types `postgres` and `redis`).

Every tool names its cluster by connector name. The namespace check happens here, against the cluster's
configured namespace (moks.datastores.require), because the namespace is part of the cluster's
configuration rather than an argument the caller chooses. A ScopeError becomes a recorded denial in the
executor, like any other.
"""
from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import Field

from .. import datastores
from ..connectors.base import ConnectorError
from ..db import session_scope
from .contracts import ToolArgs, ToolContext, ToolResult, ToolSpec

CLUSTER = r"^[a-z0-9][a-z0-9-]{0,99}$"


class ClusterArgs(ToolArgs):
    cluster: str = Field(pattern=CLUSTER, description="Connector name of the cluster (see datastore_clusters)")


class ListArgs(ToolArgs):
    kind: Literal["postgres", "redis"] | None = None
    health: bool = Field(default=True, description="Also fetch each cluster's overview and findings (at most 10 clusters)")


class SessionArgs(ClusterArgs):
    min_age_seconds: int = Field(default=0, ge=0, le=86400, description="Only sessions whose transaction or query is at least this old")
    limit: int = Field(default=50, ge=1, le=200)


class LimitArgs(ClusterArgs):
    limit: int = Field(default=20, ge=1, le=100)


class StatementArgs(ClusterArgs):
    order: Literal["total_time", "mean_time", "calls", "rows", "io"] = "total_time"
    limit: int = Field(default=15, ge=1, le=50)


class SlowlogArgs(ClusterArgs):
    limit: int = Field(default=25, ge=1, le=128)


class KeyspaceArgs(ClusterArgs):
    sample: int = Field(default=500, ge=10, le=2000)


async def _cluster(ctx: ToolContext, kind: str, name: str):
    async with session_scope() as db:
        return await datastores.resolve(db, ctx.environment, ctx.scope, ctx.principal, kind, name)


def _source(row, operation: str) -> dict:
    return {"cluster": row.name, "kind": row.type, "namespace": (row.config or {}).get("namespace"), "connector_id": str(row.id), "operation": operation}


async def datastore_clusters(ctx: ToolContext, a: ListArgs) -> ToolResult:
    async with session_scope() as db:
        rows, errors = await datastores.in_scope(db, ctx.environment, ctx.scope, ctx.principal, a.kind)
    out = [datastores.describe(r) for r, _ in rows]
    partial = False
    if a.health and rows:
        async def one(row, inst):
            try:
                ov = await asyncio.wait_for(datastores.overview_with_findings(row, inst), timeout=20)
                return {"findings": ov["findings"], "reachable": True}
            except (ConnectorError, asyncio.TimeoutError) as exc:
                return {"reachable": False, "error": exc.message if isinstance(exc, ConnectorError) else "timeout"}

        health = await asyncio.gather(*(one(r, i) for r, i in rows[:10]))
        for item, h in zip(out, health):
            item.update(h)
        partial = len(rows) > 10
    for name, msg in errors.items():
        out.append({"name": name, "reachable": False, "error": f"misconfigured: {msg}"})
    bad = sum(1 for c in out if not c.get("reachable", True) or any(f["severity"] == "bad" for f in c.get("findings") or []))
    return ToolResult(f"{len(out)} data-store cluster(s) in scope" + (f", {bad} with problems" if a.health else ""), out,
                      {"operation": "list connectors of type postgres/redis", "partial": partial})


# ---- PostgreSQL ---------------------------------------------------------------------------------

async def pg_overview(ctx: ToolContext, a: ClusterArgs) -> ToolResult:
    row, pg = await _cluster(ctx, "postgres", a.cluster)
    ov = await datastores.overview_with_findings(row, pg)
    ov["settings"] = await pg.settings()
    c = ov["connections"]
    summary = (f"{a.cluster}: {ov['server']['role']}, {c['total']}/{c['max']} connections ({c['used_pct']}%), "
               f"{ov['blocked_sessions']} blocked, {len(ov['findings'])} finding(s)")
    return ToolResult(summary, ov, _source(row, "pg_stat_activity, pg_stat_database, pg_stat_replication, pg_settings"))


async def pg_sessions(ctx: ToolContext, a: SessionArgs) -> ToolResult:
    row, pg = await _cluster(ctx, "postgres", a.cluster)
    rows = await pg.activity(a.min_age_seconds, a.limit)
    idle = sum(1 for r in rows if str(r.get("state", "")).startswith("idle in transaction"))
    return ToolResult(f"{len(rows)} non-idle session(s) on {a.cluster}" + (f", {idle} idle in transaction" if idle else ""), rows, _source(row, "pg_stat_activity"))


async def pg_locks(ctx: ToolContext, a: ClusterArgs) -> ToolResult:
    row, pg = await _cluster(ctx, "postgres", a.cluster)
    data = await pg.locks()
    roots = data["root_blockers"]
    summary = (f"{len(data['blocked'])} blocked session(s) on {a.cluster}; root blocker pid {roots[0]['blocker'].get('pid')} "
               f"({roots[0]['blocker'].get('state')}) holds up {roots[0]['waiting']}") if roots else f"No lock waits on {a.cluster}"
    return ToolResult(summary, data, _source(row, "pg_stat_activity + pg_blocking_pids + pg_locks"))


async def pg_replication(ctx: ToolContext, a: ClusterArgs) -> ToolResult:
    row, pg = await _cluster(ctx, "postgres", a.cluster)
    data = await pg.replication()
    if data["role"] == "standby":
        s = data["standby"]
        summary = f"{a.cluster} is a standby: receiver {s.get('receiver_status')}, last replay {s.get('replay_delay_s') or 0:.0f}s ago"
    else:
        summary = f"{a.cluster} is a primary with {len(data['replicas'])} replica(s) and {len(data['slots'])} slot(s)"
    return ToolResult(summary, data, _source(row, "pg_stat_replication, pg_replication_slots, pg_stat_wal_receiver"))


async def pg_maintenance(ctx: ToolContext, a: LimitArgs) -> ToolResult:
    row, pg = await _cluster(ctx, "postgres", a.cluster)
    data = await pg.maintenance(a.limit)
    worst = max((d.get("xid_age") or 0 for d in data["databases"]), default=0)
    dead = data["tables"][0] if data["tables"] else None
    summary = f"{a.cluster}: oldest xid age {worst:,}" + (f"; most dead rows in {dead['schema']}.{dead['table']} ({dead['dead_rows']:,}, {dead['dead_pct']}%)" if dead else "")
    return ToolResult(summary, data, _source(row, "pg_database, pg_stat_user_tables, pg_class, pg_stat_progress_vacuum"))


async def pg_top_queries(ctx: ToolContext, a: StatementArgs) -> ToolResult:
    row, pg = await _cluster(ctx, "postgres", a.cluster)
    data = await pg.top_statements(a.order, a.limit)
    if not data["available"]:
        return ToolResult(f"{a.cluster}: {data['note']}", data, _source(row, "pg_stat_statements"))
    return ToolResult(f"top {len(data['statements'])} statements on {a.cluster} by {a.order}", data, _source(row, "pg_stat_statements"))


# ---- Redis --------------------------------------------------------------------------------------

async def redis_overview(ctx: ToolContext, a: ClusterArgs) -> ToolResult:
    row, r = await _cluster(ctx, "redis", a.cluster)
    ov = await datastores.overview_with_findings(row, r)
    up = sum(1 for n in ov["nodes"] if n.get("reachable"))
    summary = f"{a.cluster} ({ov['mode']}): {up}/{len(ov['nodes'])} node(s) reachable, {len(ov['findings'])} finding(s)"
    if ov.get("cluster"):
        summary += f", cluster_state {ov['cluster'].get('cluster_state')}"
    return ToolResult(summary, ov, {**_source(row, "INFO, CONFIG GET, CLUSTER INFO/NODES"), "partial": ov.get("partial", False)})


async def redis_slowlog(ctx: ToolContext, a: SlowlogArgs) -> ToolResult:
    row, r = await _cluster(ctx, "redis", a.cluster)
    entries = await r.slowlog(a.limit)
    try:
        latency = await r.latency()
    except ConnectorError as exc:
        latency = {"error": exc.message}
    slowest = max((e["duration_us"] for e in entries), default=0)
    return ToolResult(f"{len(entries)} slowlog entries on {a.cluster}" + (f", slowest {slowest / 1000:.1f} ms" if entries else ""),
                      {"slowlog": entries, "latency_events": latency, "note": "Commands show the first key only; values are never read."},
                      _source(row, "SLOWLOG GET, LATENCY LATEST"))


async def redis_clients(ctx: ToolContext, a: ClusterArgs) -> ToolResult:
    row, r = await _cluster(ctx, "redis", a.cluster)
    data = await r.clients()
    return ToolResult(f"{data['total']} client connection(s) on {a.cluster}, {data['blocked']} blocked", data, _source(row, "CLIENT LIST"))


async def redis_keyspace(ctx: ToolContext, a: KeyspaceArgs) -> ToolResult:
    row, r = await _cluster(ctx, "redis", a.cluster)
    data = await r.keyspace_sample(a.sample)
    top = data["top"][0] if data.get("top") else None
    return ToolResult(f"sampled {data['sampled']} keys on {a.cluster}" + (f"; largest {top['key']} ({top['bytes']:,} bytes)" if top else ""), data,
                      _source(row, "SCAN + MEMORY USAGE (sampled)"))


TOOLS: list[ToolSpec] = [
    ToolSpec("datastore_clusters", "PostgreSQL and Redis/Valkey clusters configured in this environment that you may read, with reachability and health findings. "
             "Start here for database or cache problems: it gives the cluster names the other pg_* and redis_* tools take.",
             "datastore", ListArgs, datastore_clusters, namespace_arg=None),
    ToolSpec("pg_overview", "One PostgreSQL cluster: role (primary/standby), connection use by state and client, long transactions, blocked sessions, "
             "replication and slots, transaction-ID age, cache hit ratio, key settings and findings.", "postgres", ClusterArgs, pg_overview, namespace_arg=None),
    ToolSpec("pg_sessions", "Non-idle PostgreSQL sessions (active, idle in transaction, waiting), oldest first, with wait events and normalised query text.",
             "postgres", SessionArgs, pg_sessions, namespace_arg=None),
    ToolSpec("pg_locks", "PostgreSQL lock waits as blocking chains: the root blocker sessions, how many sessions each holds up, and lock modes.",
             "postgres", ClusterArgs, pg_locks, namespace_arg=None),
    ToolSpec("pg_replication", "PostgreSQL replication: replicas with write/flush/replay lag, replication slots and retained WAL, or standby receive/replay state.",
             "postgres", ClusterArgs, pg_replication, namespace_arg=None),
    ToolSpec("pg_maintenance", "PostgreSQL vacuum health: per-database transaction-ID age and size, tables with most dead rows and their last (auto)vacuum, "
             "oldest relations by xid age, and vacuums running now.", "postgres", LimitArgs, pg_maintenance, namespace_arg=None),
    ToolSpec("pg_top_queries", "Most expensive normalised statements from pg_stat_statements (by total or mean time, calls, rows or I/O).",
             "postgres", StatementArgs, pg_top_queries, namespace_arg=None),
    ToolSpec("redis_overview", "One Redis/Valkey deployment (standalone, Sentinel or cluster): per-node memory vs maxmemory, fragmentation, clients, hit ratio, "
             "evictions, persistence status, replication links, cluster slot state, config and findings.", "redis", ClusterArgs, redis_overview, namespace_arg=None),
    ToolSpec("redis_slowlog", "Redis SLOWLOG entries (command and first key only) and LATENCY LATEST events.", "redis", SlowlogArgs, redis_slowlog, namespace_arg=None),
    ToolSpec("redis_clients", "Redis client connections grouped by name/source, blocked clients, longest idle and largest output buffers.",
             "redis", ClusterArgs, redis_clients, namespace_arg=None),
    ToolSpec("redis_keyspace", "Largest keys and key prefixes from a bounded SCAN sample with MEMORY USAGE (approximate; key names hashed unless allowed).",
             "redis", KeyspaceArgs, redis_keyspace, namespace_arg=None),
]
