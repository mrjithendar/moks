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

ROLES = ("moks_api", "moks_worker", "moks_executor", "moks_backup", "moks_terminal")

_API_RW = ("learning_records", "learning_judgments", "learning_datasets", "learning_dataset_items", "ml_capabilities", "ml_models", "ml_artifacts", "ml_observations", "retrieval_feedback", "improvement_proposals", "users", "role_bindings", "sessions", "environments", "connectors", "llm_configs", "llm_usage", "investigations", "investigation_events", "evidence",
           "incidents", "incident_events", "jobs", "knowledge_sources", "documents", "chunks", "embedding_indexes", "conversations", "conversation_participants", "conversation_reads", "knowledge_sync_items", "document_index_state", "skill_versions", "skill_generations",
           "conversation_messages", "backup_stores", "database_backups", "database_backup_schedules", "skill_policies", "detection_policies", "detection_states", "maintenance_windows", "saved_queries",
           "diagnostic_sessions", "diagnostic_commands", "alert_events", "eval_runs", "index_outbox", "llm_reservations", "component_versions")
_WORKER_RW = ("learning_records", "ml_models", "ml_artifacts", "ml_observations", "search_backend_status", "llm_usage", "investigations", "investigation_checkpoints", "investigation_events", "evidence", "incidents", "incident_events", "jobs", "knowledge_sources", "documents", "chunks",
              "embedding_indexes", "knowledge_sync_items", "document_index_state", "skill_versions", "skill_generations", "conversations", "conversation_participants", "conversation_messages", "detection_runs", "detection_states", "alert_events", "eval_runs", "index_outbox", "llm_reservations", "component_versions")
_EXECUTOR_RW = ("diagnostic_sessions", "diagnostic_commands", "incidents", "incident_events", "component_versions")

TABLES = ("learning_records", "learning_judgments", "learning_datasets", "learning_dataset_items", "ml_capabilities", "ml_models", "ml_artifacts", "ml_observations", "retrieval_feedback", "search_backend_status", "improvement_proposals", "users", "role_bindings", "sessions", "environments", "connectors", "llm_configs", "llm_usage", "investigations", "investigation_checkpoints", "investigation_events", "evidence", "audit_events",
          "incidents", "incident_events", "jobs", "knowledge_sources", "documents", "chunks", "embedding_indexes", "conversations", "conversation_participants", "conversation_reads", "knowledge_sync_items", "document_index_state", "skill_versions", "skill_generations",
          "conversation_messages", "backup_runs", "backup_stores", "database_backups", "database_backup_schedules", "skill_policies", "detection_policies", "detection_runs", "detection_states", "maintenance_windows",
          "saved_queries", "index_outbox", "llm_reservations", "component_versions", "action_plans", "approvals", "diagnostic_sessions", "diagnostic_commands", "terminal_sessions", "alert_events", "eval_runs", "alembic_version",
          "secret_mappings", "secret_inventory", "secret_inventory_scopes", "secret_plans", "secret_plan_items", "secret_plan_approvals", "secret_sync_schedules",
          "secret_schedule_approvals")
# The gateway signs people in and runs shells, so it sees only that. Its session updates are the two columns load() touches.
_TERMINAL_READS = {"users", "role_bindings", "sessions", "environments", "terminal_sessions", "alembic_version"}
SECRETS = {"secret_mappings", "secret_inventory", "secret_inventory_scopes", "secret_plans", "secret_plan_items", "secret_plan_approvals", "secret_sync_schedules",
           "secret_schedule_approvals"}  # names, key names, versions and keyed digests: the worker never needs them
LEARNING = {"learning_records", "learning_judgments", "learning_datasets", "learning_dataset_items"}  # the executor runs actions and has no use for training data
HIDDEN = {"moks_worker": {"sessions", "terminal_sessions"} | SECRETS, "moks_backup": SECRETS, "moks_executor": {"sessions", "terminal_sessions"} | LEARNING,  # session hashes are the API's business
          "moks_terminal": set(TABLES) - _TERMINAL_READS}

API_PLAN_COLUMNS = ("status", "plan_hash", "executed_by", "queued_at", "finished_at", "result")
# Secret sync. The API owns mappings and plans up to approval; the executor owns comparison results, execution
# state and baselines. Approvals are insert-only for the API, like action approvals.
API_SECRET_MAPPING_COLUMNS = ("aws_secret_name", "payload_format", "key_mapping", "k8s_type", "direction", "expected_owner", "management_policy", "status",
                              "version", "baseline", "baseline_seq", "updated_by", "updated_at")
EXECUTOR_SECRET_MAPPING_COLUMNS = ("baseline", "baseline_seq", "last_status", "last_compared_at", "aws_secret_arn", "lease_owner", "lease_expires_at")
API_SECRET_PLAN_COLUMNS = ("status", "plan_hash", "requires_approval", "expires_at", "version", "summary", "executed_by", "compare_job_id", "execute_job_id",
                           "submitted_at", "queued_at", "finished_at", "error")
EXECUTOR_SECRET_PLAN_COLUMNS = ("status", "summary", "version", "error", "finished_at")
# A scheduled run is a plan the executor creates, compares and queues itself. It may hash and queue it, but never
# waive its approval (requires_approval) or record one: the approval is the schedule's, which only the API inserts.
EXECUTOR_SCHEDULED_PLAN_COLUMNS = ("plan_hash", "expires_at", "submitted_at", "queued_at", "executed_by")
API_SECRET_SCHEDULE_COLUMNS = ("name", "mapping_ids", "mapping_versions", "interval_minutes", "status", "paused_reason", "schedule_hash", "version", "next_run_at", "updated_at")
EXECUTOR_SECRET_SCHEDULE_COLUMNS = ("status", "paused_reason", "next_run_at", "last_run_at", "last_plan_id", "last_outcome", "updated_at")
API_SECRET_ITEM_COLUMNS = ("included", "resolution", "key_choices", "targets", "actions", "expected", "diff", "warnings", "blocked", "status", "error", "updated_at")
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
    out += [f"GRANT {RW} ON terminal_sessions TO moks_terminal", "GRANT UPDATE (last_seen_at, revoked_at) ON sessions TO moks_terminal"]
    out += ["GRANT INSERT ON approvals TO moks_api", "GRANT INSERT ON action_plans TO moks_api", "GRANT INSERT ON backup_runs TO moks_backup",
            f"GRANT UPDATE ({', '.join(API_PLAN_COLUMNS)}) ON action_plans TO moks_api",
            f"GRANT UPDATE ({', '.join(EXECUTOR_PLAN_COLUMNS)}) ON action_plans TO moks_executor"]
    out += ["GRANT INSERT ON secret_mappings, secret_plans, secret_plan_items TO moks_api", "GRANT INSERT ON secret_plan_approvals TO moks_api",
            f"GRANT UPDATE ({', '.join(API_SECRET_MAPPING_COLUMNS)}) ON secret_mappings TO moks_api",
            f"GRANT UPDATE ({', '.join(API_SECRET_PLAN_COLUMNS)}) ON secret_plans TO moks_api",
            f"GRANT UPDATE ({', '.join(API_SECRET_ITEM_COLUMNS)}) ON secret_plan_items TO moks_api",
            "GRANT SELECT, INSERT, UPDATE, DELETE ON secret_inventory, secret_inventory_scopes TO moks_executor",
            f"GRANT UPDATE ({', '.join(EXECUTOR_SECRET_MAPPING_COLUMNS)}) ON secret_mappings TO moks_executor",
            f"GRANT UPDATE ({', '.join(EXECUTOR_SECRET_PLAN_COLUMNS)}) ON secret_plans TO moks_executor",
            "GRANT UPDATE ON secret_plan_items TO moks_executor",
            "GRANT INSERT ON secret_sync_schedules, secret_schedule_approvals TO moks_api",
            f"GRANT UPDATE ({', '.join(API_SECRET_SCHEDULE_COLUMNS)}) ON secret_sync_schedules TO moks_api",
            f"GRANT UPDATE ({', '.join(EXECUTOR_SECRET_SCHEDULE_COLUMNS)}) ON secret_sync_schedules TO moks_executor",
            "GRANT INSERT ON secret_plans, secret_plan_items TO moks_executor",
            f"GRANT UPDATE ({', '.join(EXECUTOR_SCHEDULED_PLAN_COLUMNS)}) ON secret_plans TO moks_executor",
            # scheduled discovery and secret reveals queue their own jobs
            "GRANT INSERT ON jobs TO moks_executor",
            # the executor runs secrets-class jobs, so it claims, renews and finishes them
            "GRANT UPDATE (status, worker_id, started_at, heartbeat_at, attempts, lease_expires_at, result, error, finished_at, run_after) ON jobs TO moks_executor"]
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
