"""PostgreSQL connector: read-only SRE views of one server (CloudNativePG, Bitnami, RDS/Aurora, or plain).

Configuration (non-secret JSON) and a credential reference holding the password:

    {"host": "shop-db-rw.shop.svc", "port": 5432, "database": "app", "username": "moks_monitor",
     "sslmode": "prefer", "namespace": "shop", "service": "shop-db-rw", "timeout_seconds": 10}

Read-only three times over: the role should hold only pg_monitor (docs/DATASTORES.md); every session runs with
default_transaction_read_only=on and a short statement_timeout and lock_timeout; and the class sends only the
statements in QUERIES, which take no SQL from callers. ORDER BY and LIMIT come from allowlists and bounds.

Query text from pg_stat_activity is normalised (literals replaced with ?) unless config.query_text is "raw",
and dropped entirely when it is "none". pg_stat_statements text is already normalised by PostgreSQL.
Needs PostgreSQL 13 or newer.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from ..observability import CONNECTOR_LATENCY, CONNECTOR_REQUESTS
from .base import ConnectorError, resolve_credential

SSLMODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
QUERY_TEXT = ("normalized", "raw", "none")
MAX_ROWS = 200

SERVER_SETTINGS = {
    "application_name": "moks",
    "default_transaction_read_only": "on",
    "statement_timeout": "5000",
    "lock_timeout": "500",
    "idle_in_transaction_session_timeout": "10000",
}

WATCHED_SETTINGS = ["max_connections", "superuser_reserved_connections", "shared_buffers", "work_mem", "maintenance_work_mem",
                    "effective_cache_size", "max_wal_size", "wal_level", "max_replication_slots", "max_slot_wal_keep_size",
                    "hot_standby_feedback", "autovacuum", "autovacuum_max_workers", "autovacuum_vacuum_scale_factor",
                    "idle_in_transaction_session_timeout", "statement_timeout", "log_min_duration_statement", "shared_preload_libraries"]

QUERIES: dict[str, str] = {
    "server": """
        SELECT version() AS version, current_setting('server_version_num')::int AS version_num,
               pg_postmaster_start_time() AS started_at, pg_is_in_recovery() AS in_recovery, current_database() AS database,
               current_setting('max_connections')::int AS max_connections,
               current_setting('superuser_reserved_connections')::int AS reserved_connections""",
    "connections_by_state": """
        SELECT coalesce(state, 'unknown') AS state, count(*)::int AS n
        FROM pg_stat_activity WHERE backend_type = 'client backend' GROUP BY 1 ORDER BY 2 DESC""",
    "connections_by_source": """
        SELECT datname AS database, usename AS "user", application_name AS application, count(*)::int AS n,
               count(*) FILTER (WHERE state = 'idle in transaction')::int AS idle_in_transaction
        FROM pg_stat_activity WHERE backend_type = 'client backend' GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 25""",
    "activity": """
        SELECT pid, datname AS database, usename AS "user", application_name AS application, client_addr::text AS client,
               state, wait_event_type, wait_event, backend_start, xact_start, query_start,
               extract(epoch FROM now() - xact_start)::float AS xact_age_s, extract(epoch FROM now() - query_start)::float AS query_age_s,
               extract(epoch FROM now() - state_change)::float AS state_age_s,
               left(query, 2000) AS query, backend_xid::text AS xid, backend_xmin::text AS xmin
        FROM pg_stat_activity
        WHERE backend_type = 'client backend' AND pid <> pg_backend_pid() AND state IS DISTINCT FROM 'idle'
          AND coalesce(extract(epoch FROM now() - coalesce(xact_start, query_start)), 0) >= $1
        ORDER BY coalesce(xact_start, query_start) NULLS LAST LIMIT $2""",
    "blocked": """
        SELECT pid, pg_blocking_pids(pid) AS blocked_by, datname AS database, usename AS "user", application_name AS application,
               state, wait_event_type, wait_event, extract(epoch FROM now() - query_start)::float AS waiting_s, left(query, 1000) AS query
        FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0 ORDER BY query_start LIMIT 100""",
    "sessions_by_pid": """
        SELECT pid, datname AS database, usename AS "user", application_name AS application, client_addr::text AS client, state,
               backend_type, query_start, (SELECT rolsuper FROM pg_roles WHERE rolname = usename) AS superuser,
               wait_event_type, wait_event, backend_start, extract(epoch FROM now() - xact_start)::float AS xact_age_s,
               extract(epoch FROM now() - query_start)::float AS query_age_s, left(query, 1000) AS query
        FROM pg_stat_activity WHERE pid = ANY($1::int[])""",
    "lock_modes": """
        SELECT l.pid, l.locktype, l.mode, l.granted, l.relation::regclass::text AS relation
        FROM pg_locks l WHERE l.pid = ANY($1::int[]) AND l.locktype IN ('relation', 'transactionid', 'tuple', 'advisory') LIMIT 200""",
    "replication_primary": """
        SELECT application_name, client_addr::text AS client, state, sync_state,
               pg_wal_lsn_diff(pg_current_wal_lsn(), sent_lsn)::bigint AS send_lag_bytes,
               pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::bigint AS replay_lag_bytes,
               extract(epoch FROM write_lag)::float AS write_lag_s, extract(epoch FROM flush_lag)::float AS flush_lag_s,
               extract(epoch FROM replay_lag)::float AS replay_lag_s
        FROM pg_stat_replication ORDER BY application_name""",
    "slots_primary": """
        SELECT slot_name, slot_type, active, database, wal_status,
               pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)::bigint AS retained_bytes
        FROM pg_replication_slots ORDER BY retained_bytes DESC NULLS LAST""",
    "replication_standby": """
        SELECT pg_last_wal_receive_lsn()::text AS receive_lsn, pg_last_wal_replay_lsn()::text AS replay_lsn,
               pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn())::bigint AS apply_backlog_bytes,
               extract(epoch FROM now() - pg_last_xact_replay_timestamp())::float AS replay_delay_s,
               (SELECT status FROM pg_stat_wal_receiver LIMIT 1) AS receiver_status,
               (SELECT sender_host FROM pg_stat_wal_receiver LIMIT 1) AS upstream""",
    "databases": """
        SELECT d.datname AS database, age(d.datfrozenxid)::bigint AS xid_age, mxid_age(d.datminmxid)::bigint AS mxid_age,
               CASE WHEN has_database_privilege(d.datname, 'CONNECT') THEN pg_database_size(d.datname) END::bigint AS size_bytes,
               s.numbackends, s.xact_commit, s.xact_rollback, s.deadlocks, s.temp_files, s.temp_bytes, s.conflicts, s.blks_hit, s.blks_read
        FROM pg_database d LEFT JOIN pg_stat_database s ON s.datid = d.oid
        WHERE d.datallowconn ORDER BY xid_age DESC LIMIT 50""",
    "tables": """
        SELECT schemaname AS schema, relname AS "table", n_live_tup AS live_rows, n_dead_tup AS dead_rows,
               round(100.0 * n_dead_tup / greatest(n_live_tup + n_dead_tup, 1), 1)::float AS dead_pct,
               last_autovacuum, last_vacuum, last_autoanalyze, autovacuum_count, seq_scan, idx_scan,
               pg_total_relation_size(relid)::bigint AS total_bytes
        FROM pg_stat_user_tables ORDER BY n_dead_tup DESC, total_bytes DESC LIMIT $1""",
    "table_xid_age": """
        SELECT c.oid::regclass::text AS relation, age(c.relfrozenxid)::bigint AS xid_age
        FROM pg_class c WHERE c.relkind IN ('r', 'm', 't') ORDER BY 2 DESC LIMIT 10""",
    "vacuum_progress": """
        SELECT p.pid, p.datname AS database, p.relid::regclass::text AS relation, p.phase,
               p.heap_blks_total, p.heap_blks_scanned, extract(epoch FROM now() - a.xact_start)::float AS running_s
        FROM pg_stat_progress_vacuum p LEFT JOIN pg_stat_activity a ON a.pid = p.pid""",
    "server_identity": "SELECT system_identifier::text AS system_identifier FROM pg_control_system()",
    "has_pg_stat_statements": "SELECT count(*)::int AS n FROM pg_extension WHERE extname = 'pg_stat_statements'",
    "settings": "SELECT name, setting, unit, source, pending_restart FROM pg_settings WHERE name = ANY($1::text[]) ORDER BY name",
}

# The only statements that change anything. They run only on an operator connector the executor builds
# (moks/actions/datastore.py), still inside a read-only session (signalling a backend writes no data), and they act
# only on the exact session that was reviewed: same pid AND same backend_start, and only a client backend.
SIGNALS = {
    "cancel": """SELECT pg_cancel_backend(pid) AS signalled FROM pg_stat_activity
                 WHERE pid = $1 AND backend_start = $2::timestamptz AND backend_type = 'client backend'""",
    "terminate": """SELECT pg_terminate_backend(pid) AS signalled FROM pg_stat_activity
                    WHERE pid = $1 AND backend_start = $2::timestamptz AND backend_type = 'client backend'""",
}

# ORDER BY for pg_stat_statements comes from this map, never from the caller.
STATEMENT_ORDER = {"total_time": "total_exec_time", "mean_time": "mean_exec_time", "calls": "calls", "rows": "rows",
                   "io": "shared_blks_read + temp_blks_written"}
STATEMENTS_SQL = """
    SELECT queryid::text AS query_id, calls, round(total_exec_time::numeric, 1)::float AS total_ms,
           round(mean_exec_time::numeric, 2)::float AS mean_ms, rows, shared_blks_hit, shared_blks_read, temp_blks_written,
           left(query, 1000) AS query
    FROM {schema}.pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
    ORDER BY {order} DESC LIMIT $1"""

_STRING = re.compile(r"(?s)'(?:[^']|'')*'|\$([A-Za-z_]*)\$.*?\$\1\$")
_NUMBER = re.compile(r"(?<![\w$.])-?\b\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\b")
_IN_LIST = re.compile(r"\(\s*\?(?:\s*,\s*\?)+\s*\)")


def normalize_sql(text: str | None) -> str | None:
    """Replace literals with ? so query text can be shown without the values it carried (emails, tokens, IDs)."""
    if text is None:
        return None
    out = _STRING.sub("?", text)
    out = _NUMBER.sub("?", out)
    out = _IN_LIST.sub("(?, …)", out)
    return re.sub(r"\s+", " ", out).strip()


def _jsonable(v: Any) -> Any:
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    return str(v)


class PostgresConnector:
    type = "postgres"

    def __init__(self, config: dict[str, Any], credential_ref: dict[str, Any] | None = None):
        self.host = str(config.get("host") or "")
        if not self.host:
            raise ConnectorError("config", "postgres connector needs config.host (a Service DNS name or address)")
        self.port = int(config.get("port") or 5432)
        self.database = str(config.get("database") or "postgres")
        self.username = str(config.get("username") or "")
        if not self.username:
            raise ConnectorError("config", "postgres connector needs config.username (a role with pg_monitor)")
        self.sslmode = config.get("sslmode", "prefer")
        if self.sslmode not in SSLMODES:
            raise ConnectorError("config", f"sslmode must be one of {', '.join(SSLMODES)}")
        self.query_text = config.get("query_text", "normalized")
        if self.query_text not in QUERY_TEXT:
            raise ConnectorError("config", f"query_text must be one of {', '.join(QUERY_TEXT)}")
        self.timeout = float(config.get("timeout_seconds", 10))
        self.statement_timeout_ms = max(500, min(int(config.get("statement_timeout_ms", 5000)), 30000))
        self.namespace = config.get("namespace")
        self._password = resolve_credential(credential_ref)
        self._conn: Any = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await asyncio.wait_for(conn.close(), timeout=5)
            except Exception:
                conn.terminate()

    async def _connect(self) -> Any:
        import asyncpg

        try:
            return await asyncpg.connect(
                host=self.host, port=self.port, user=self.username, password=self._password, database=self.database,
                ssl=self.sslmode, timeout=min(self.timeout, 10), command_timeout=self.timeout, statement_cache_size=0,
                server_settings={**SERVER_SETTINGS, "statement_timeout": str(self.statement_timeout_ms)},
            )
        except (asyncpg.InvalidPasswordError, asyncpg.InvalidAuthorizationSpecificationError):
            raise ConnectorError("auth", f"postgres rejected the credentials for role {self.username!r}") from None
        except asyncpg.InvalidCatalogNameError:
            raise ConnectorError("not_found", f"database {self.database!r} does not exist") from None
        except asyncpg.TooManyConnectionsError:
            raise ConnectorError("unavailable", "postgres refused the connection: too many connections", retryable=True) from None
        except asyncpg.PostgresError as exc:
            raise ConnectorError("bad_request", f"postgres refused the connection ({exc.sqlstate}: {str(exc)[:200]})") from None
        except asyncio.TimeoutError:
            raise ConnectorError("timeout", f"postgres at {self.host}:{self.port} did not answer within {min(self.timeout, 10):.0f}s", retryable=True) from None
        except OSError as exc:
            raise ConnectorError("unavailable", f"cannot reach postgres at {self.host}:{self.port} ({type(exc).__name__})", retryable=True) from None
        except Exception as exc:  # asyncpg raises its own ConnectionError subclasses for TLS refusals
            raise ConnectorError("unavailable", f"cannot connect to postgres at {self.host}:{self.port} ({type(exc).__name__}: {str(exc)[:200]})", retryable=True) from None

    async def fetch(self, name: str, *args: Any, sql: str | None = None) -> list[dict[str, Any]]:
        """Run one catalog statement. `sql` is only ever a formatted catalog template (pg_stat_statements)."""
        import asyncpg

        statement = sql if sql is not None else QUERIES[name]
        start = time.monotonic()
        last: ConnectorError | None = None
        for attempt in range(2):
            try:
                async with self._lock:
                    if self._conn is None or self._conn.is_closed():
                        self._conn = await self._connect()
                    rows = await self._conn.fetch(statement, *args, timeout=self.timeout)
                CONNECTOR_REQUESTS.labels("postgres", name, "ok").inc()
                CONNECTOR_LATENCY.labels("postgres", name).observe(time.monotonic() - start)
                return [{k: _jsonable(v) for k, v in dict(r).items()} for r in rows[:MAX_ROWS]]
            except ConnectorError as exc:
                last = exc
            except asyncpg.InsufficientPrivilegeError:
                last = ConnectorError("forbidden", f"the role {self.username!r} may not read {name}; grant pg_monitor (docs/DATASTORES.md)")
            except asyncpg.QueryCanceledError:
                last = ConnectorError("timeout", f"postgres cancelled {name} after {self.statement_timeout_ms} ms (statement_timeout)")
            except asyncpg.LockNotAvailableError:
                last = ConnectorError("timeout", f"{name} waited too long for a lock (lock_timeout)", retryable=True)
            except asyncpg.UndefinedTableError as exc:
                last = ConnectorError("not_found", f"{name}: {str(exc)[:200]}")
            except asyncpg.PostgresError as exc:
                last = ConnectorError("bad_request", f"{name} failed ({exc.sqlstate}: {str(exc)[:200]})")
            except (asyncpg.InterfaceError, OSError, asyncio.TimeoutError) as exc:
                await self.aclose()
                last = ConnectorError("unavailable" if not isinstance(exc, asyncio.TimeoutError) else "timeout",
                                      f"postgres connection lost during {name} ({type(exc).__name__})", retryable=True)
            if not last.retryable or attempt == 1:
                break
            await asyncio.sleep(0.3)
        assert last is not None
        CONNECTOR_REQUESTS.labels("postgres", name, last.kind).inc()
        raise last

    def _text(self, q: str | None) -> str | None:
        if self.query_text == "none":
            return None
        return q if self.query_text == "raw" else normalize_sql(q)

    # ---- views -------------------------------------------------------------------------------

    async def test(self) -> str:
        s = (await self.fetch("server"))[0]
        role = "standby" if s["in_recovery"] else "primary"
        version = str(s["version"]).split(" on ")[0].split(",")[0]
        if s["version_num"] < 130000:
            return f"{version} reachable as {role}, but MOKS needs PostgreSQL 13 or newer: some views will fail"
        try:
            await self.fetch("databases")
        except ConnectorError as exc:
            return f"{version} reachable as {role}, but {exc.message}"
        return f"{version} reachable as {role} (database {s['database']})"

    async def overview(self) -> dict[str, Any]:
        server = (await self.fetch("server"))[0]
        by_state = await self.fetch("connections_by_state")
        total = sum(r["n"] for r in by_state)
        usable = max(1, server["max_connections"] - server["reserved_connections"])
        dbs = await self.fetch("databases")
        hit = sum(d.get("blks_hit") or 0 for d in dbs)
        read = sum(d.get("blks_read") or 0 for d in dbs)
        blocked = await self.fetch("blocked")
        long = await self.fetch("activity", 300.0, 20)
        out = {
            "server": {**server, "role": "standby" if server["in_recovery"] else "primary"},
            "connections": {"total": total, "max": server["max_connections"], "reserved": server["reserved_connections"],
                            "used_pct": round(100 * total / usable, 1), "by_state": {r["state"]: r["n"] for r in by_state},
                            "by_source": await self.fetch("connections_by_source")},
            "cache_hit_ratio": round(hit / (hit + read), 4) if hit + read else None,
            "deadlocks_total": sum(d.get("deadlocks") or 0 for d in dbs),
            "oldest_xid_age": max((d.get("xid_age") or 0 for d in dbs), default=0),
            "blocked_sessions": len(blocked),
            "long_transactions": [self._session(r) for r in long if (r.get("xact_age_s") or 0) >= 300][:10],
            "idle_in_transaction": sum(r["n"] for r in by_state if r["state"].startswith("idle in transaction")),
        }
        out["replication"] = await self.replication()
        return out

    def _session(self, r: dict[str, Any]) -> dict[str, Any]:
        return {**{k: v for k, v in r.items() if k != "query"}, "query": self._text(r.get("query"))}

    async def activity(self, min_age_seconds: float = 0, limit: int = 50) -> list[dict[str, Any]]:
        return [self._session(r) for r in await self.fetch("activity", float(min_age_seconds), max(1, min(limit, MAX_ROWS)))]

    async def locks(self) -> dict[str, Any]:
        """Blocking chains: who waits on whom, with the root blockers (often an idle-in-transaction session)."""
        blocked = await self.fetch("blocked")
        waiting = {r["pid"] for r in blocked}
        blockers = sorted({b for r in blocked for b in r["blocked_by"] or []})
        roots = [b for b in blockers if b not in waiting]
        sessions = {r["pid"]: self._session(r) for r in await self.fetch("sessions_by_pid", roots)} if roots else {}
        modes = await self.fetch("lock_modes", sorted(waiting | set(blockers))) if waiting else []
        tree = []
        for root in roots:
            victims = _victims(root, blocked)
            tree.append({"blocker": sessions.get(root, {"pid": root, "note": "session ended or not visible"}), "waiting": len(victims),
                         "waiting_pids": victims[:50], "locks_held": [m for m in modes if m["pid"] == root and m["granted"]][:10]})
        tree.sort(key=lambda t: -t["waiting"])
        return {"blocked": [self._session(r) for r in blocked], "root_blockers": tree,
                "waiting_on": [m for m in modes if not m["granted"]][:50]}

    async def replication(self) -> dict[str, Any]:
        server = (await self.fetch("server"))[0]
        if server["in_recovery"]:
            standby = (await self.fetch("replication_standby"))[0]
            return {"role": "standby", "standby": standby, "replicas": [], "slots": []}
        return {"role": "primary", "replicas": await self.fetch("replication_primary"), "slots": await self.fetch("slots_primary")}

    async def maintenance(self, limit: int = 20) -> dict[str, Any]:
        return {"databases": await self.fetch("databases"), "tables": await self.fetch("tables", max(1, min(limit, 100))),
                "oldest_tables_by_xid": await self.fetch("table_xid_age"), "vacuum_running": await self.fetch("vacuum_progress")}

    async def top_statements(self, order: str = "total_time", limit: int = 15) -> dict[str, Any]:
        if order not in STATEMENT_ORDER:
            raise ConnectorError("bad_request", f"order must be one of {', '.join(STATEMENT_ORDER)}")
        rows = await self.fetch("has_pg_stat_statements")
        if not rows or not rows[0]["n"]:
            return {"available": False, "note": "pg_stat_statements is not installed in this database: add it to shared_preload_libraries and CREATE EXTENSION",
                    "statements": []}
        schema = (await self.fetch("pgss_schema", sql="SELECT n.nspname AS schema FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
                                                       "WHERE e.extname = 'pg_stat_statements'"))[0]["schema"]
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
            raise ConnectorError("bad_request", "pg_stat_statements is installed in a schema MOKS will not quote")
        stmts = await self.fetch("statements", max(1, min(limit, 50)), sql=STATEMENTS_SQL.format(schema=schema, order=STATEMENT_ORDER[order]))
        return {"available": True, "order": order, "statements": [{**s, "query": None if self.query_text == "none" else s["query"]} for s in stmts]}

    async def settings(self) -> list[dict[str, Any]]:
        return await self.fetch("settings", WATCHED_SETTINGS)

    async def server_identity(self) -> str:
        """The cluster's system identifier: the same for a primary and its physical replicas, different for any other cluster."""
        return str((await self.fetch("server_identity"))[0]["system_identifier"])

    async def session(self, pid: int) -> dict[str, Any] | None:
        rows = await self.fetch("sessions_by_pid", [int(pid)])
        return self._session(rows[0]) if rows else None

    async def signal(self, kind: str, pid: int, backend_start: str) -> bool:
        """Cancel or terminate one reviewed session. True when PostgreSQL accepted the signal; False when that session is gone
        or is not the one reviewed. Never retried: a lost answer is for a person to reconcile."""
        from datetime import datetime

        import asyncpg

        if kind not in SIGNALS:
            raise ConnectorError("bad_request", f"unknown signal {kind!r}")
        try:
            started = datetime.fromisoformat(backend_start)
        except ValueError:
            raise ConnectorError("invalid_args", "backend_start is not a timestamp") from None
        try:
            async with self._lock:
                if self._conn is None or self._conn.is_closed():
                    self._conn = await self._connect()
                rows = await self._conn.fetch(SIGNALS[kind], int(pid), started, timeout=self.timeout)
        except asyncpg.InsufficientPrivilegeError:
            raise ConnectorError("forbidden", f"the role {self.username!r} may not signal this session; grant pg_signal_backend (it can never signal a superuser)") from None
        except asyncpg.PostgresError as exc:
            raise ConnectorError("bad_request", f"{kind} failed ({exc.sqlstate}: {str(exc)[:200]})") from None
        except (asyncpg.InterfaceError, OSError, asyncio.TimeoutError) as exc:
            await self.aclose()
            raise ConnectorError("timeout" if isinstance(exc, asyncio.TimeoutError) else "unavailable", f"{kind}: connection lost ({type(exc).__name__})") from None
        return bool(rows and rows[0]["signalled"])


def _victims(root: int, blocked: list[dict[str, Any]]) -> list[int]:
    """Every pid waiting on root, directly or through another waiter."""
    out: list[int] = []
    frontier = [root]
    seen = {root}
    while frontier:
        nxt = []
        for r in blocked:
            if r["pid"] not in seen and any(b in frontier for b in r["blocked_by"] or []):
                seen.add(r["pid"])
                out.append(r["pid"])
                nxt.append(r["pid"])
        frontier = nxt
    return out
