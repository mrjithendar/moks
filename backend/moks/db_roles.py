"""Database roles per component. `moks db-grants` applies this; the migration owner runs it.

Runtime pods never hold the schema owner's credentials. Each component gets only the table
operations its job needs, and every table must be classified here (a test enforces that), so a new
table is denied until someone decides who may touch it.

action_plans is guarded twice. Column grants stop the API writing execution state such as the
lease, token or checkpoint, and a trigger (migration 0014) limits which status changes the API may
make. Approvals are insert-only for the API and read-only for everyone else, so a decision that was
recorded cannot be changed. The audit log's own triggers already refuse UPDATE and DELETE.
"""
from __future__ import annotations

import os
from typing import Any

RW = "SELECT, INSERT, UPDATE, DELETE"
READ = "SELECT"
APPEND = "SELECT, INSERT"

ROLES = ("moks_api", "moks_worker", "moks_executor", "moks_backup")

_API_RW = ("users", "role_bindings", "sessions", "environments", "connectors", "llm_configs", "llm_usage", "investigations", "investigation_events", "evidence",
           "incidents", "incident_events", "jobs", "knowledge_sources", "documents", "chunks", "embedding_indexes", "conversations", "conversation_participants", "conversation_reads",
           "conversation_messages", "backup_stores", "skill_policies", "detection_policies", "detection_states", "maintenance_windows", "saved_queries",
           "diagnostic_sessions", "diagnostic_commands", "alert_events", "eval_runs", "index_outbox", "llm_reservations", "component_versions")
_WORKER_RW = ("llm_usage", "investigations", "investigation_events", "evidence", "incidents", "incident_events", "jobs", "knowledge_sources", "documents", "chunks",
              "embedding_indexes", "conversations", "conversation_participants", "conversation_messages", "detection_runs", "detection_states", "alert_events", "eval_runs", "index_outbox", "llm_reservations", "component_versions")
_EXECUTOR_RW = ("diagnostic_sessions", "diagnostic_commands", "incidents", "incident_events", "component_versions")

TABLES = ("users", "role_bindings", "sessions", "environments", "connectors", "llm_configs", "llm_usage", "investigations", "investigation_events", "evidence", "audit_events",
          "incidents", "incident_events", "jobs", "knowledge_sources", "documents", "chunks", "embedding_indexes", "conversations", "conversation_participants", "conversation_reads",
          "conversation_messages", "backup_runs", "backup_stores", "skill_policies", "detection_policies", "detection_runs", "detection_states", "maintenance_windows",
          "saved_queries", "index_outbox", "llm_reservations", "component_versions", "action_plans", "approvals", "diagnostic_sessions", "diagnostic_commands", "alert_events", "eval_runs", "alembic_version")
HIDDEN = {"moks_worker": {"sessions"}, "moks_executor": {"sessions"}}  # session hashes are the API's business

API_PLAN_COLUMNS = ("status", "plan_hash", "executed_by", "queued_at", "finished_at", "result")
EXECUTOR_PLAN_COLUMNS = ("status", "worker_id", "executed_at", "heartbeat_at", "lease_expires_at", "execution_token", "mutation_state", "checkpoint", "finished_at", "result")


def statements() -> list[str]:
    roles = ", ".join(ROLES)
    out = [f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {roles}", f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {roles}",
           f"GRANT USAGE ON SCHEMA public TO {roles}", f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {roles}"]
    for role in ROLES:
        hidden = HIDDEN.get(role, set())
        out += [f"GRANT {READ} ON {t} TO {role}" for t in TABLES if t not in hidden]
    out += [f"GRANT {RW} ON {t} TO moks_api" for t in _API_RW]
    out += [f"GRANT {RW} ON {t} TO moks_worker" for t in _WORKER_RW]
    out += [f"GRANT {RW} ON {t} TO moks_executor" for t in _EXECUTOR_RW]
    out += [f"GRANT {APPEND} ON audit_events TO {r}" for r in ROLES]
    out += ["GRANT INSERT ON approvals TO moks_api", "GRANT INSERT ON action_plans TO moks_api", "GRANT INSERT ON backup_runs TO moks_backup",
            f"GRANT UPDATE ({', '.join(API_PLAN_COLUMNS)}) ON action_plans TO moks_api",
            f"GRANT UPDATE ({', '.join(EXECUTOR_PLAN_COLUMNS)}) ON action_plans TO moks_executor"]
    return out


async def apply(conn: Any, database: str) -> list[str]:
    """Idempotent. Returns role names whose password was set; values never appear in output."""
    changed: list[str] = []
    for role in ROLES:
        exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
        if not exists:
            await conn.execute(f"CREATE ROLE {role} NOLOGIN")
        password = os.environ.get(f"MOKS_DB_PASSWORD_{role.removeprefix('moks_').upper()}")
        if password:
            sql = await conn.fetchval("SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', $1::text, $2::text)", role, password)
            await conn.execute(sql)
            changed.append(role)
        await conn.execute(f'GRANT CONNECT ON DATABASE "{database}" TO {role}')
    for sql in statements():
        await conn.execute(sql)
    if await conn.fetchval("SELECT to_regclass('public.chunk_embeddings') IS NOT NULL"):  # only with pgvector
        for role in ("moks_api", "moks_worker"):
            await conn.execute(f"GRANT {RW} ON chunk_embeddings TO {role}")
        await conn.execute("GRANT SELECT ON chunk_embeddings TO moks_executor, moks_backup")
    return changed
