"""Skill-driven detection and incidents, durable sync schedules, investigation threads.

Existing sources keep any interval they set explicitly; the rest move to daily. Detection is off
until enabled per environment.

Revision ID: 0007
Revises: 0006
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for stmt in (
        "ALTER TABLE investigations ADD COLUMN IF NOT EXISTS parent_id uuid",
        "ALTER TABLE investigations ADD COLUMN IF NOT EXISTS group_id uuid",
        "ALTER TABLE investigations ADD COLUMN IF NOT EXISTS stage varchar(20) NOT NULL DEFAULT 'queued'",
        "ALTER TABLE knowledge_sources ADD COLUMN IF NOT EXISTS sync_interval_minutes integer NOT NULL DEFAULT 1440",
        "ALTER TABLE knowledge_sources ADD COLUMN IF NOT EXISTS next_sync_at timestamptz",
        "ALTER TABLE knowledge_sources ADD COLUMN IF NOT EXISTS last_success_at timestamptz",
        "ALTER TABLE knowledge_sources ADD COLUMN IF NOT EXISTS last_attempt_at timestamptz",
        "ALTER TABLE knowledge_sources ADD COLUMN IF NOT EXISTS last_counts jsonb",
        """UPDATE knowledge_sources SET sync_interval_minutes = (config->>'sync_interval_minutes')::int
           WHERE config->>'sync_interval_minutes' ~ '^[0-9]+$'""",
        """UPDATE knowledge_sources SET last_success_at = last_sync_at WHERE last_sync_status = 'ok'""",
        """UPDATE knowledge_sources SET next_sync_at = coalesce(last_sync_at, now()) + make_interval(mins => sync_interval_minutes)
           WHERE type = 'github' AND sync_interval_minutes > 0""",
        "ALTER TABLE environments ADD COLUMN IF NOT EXISTS detection_enabled boolean NOT NULL DEFAULT false",
        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS rule_id varchar(120)",
        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS rule_version varchar(20)",
        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS skill varchar(120)",
        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS resource varchar(300)",
        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS first_observed_at timestamptz",
        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS last_observed_at timestamptz",
        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS detection_count integer NOT NULL DEFAULT 0",
        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS owner_team varchar(120)",
        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS severity_reason text",
        "ALTER TABLE incidents ADD COLUMN IF NOT EXISTS runbook text",
        "CREATE UNIQUE INDEX IF NOT EXISTS incidents_one_active_detection ON incidents (environment_id, fingerprint) WHERE source = 'detection' AND status IN ('open','mitigated')",
        """CREATE TABLE IF NOT EXISTS detection_policies (
            rule_id varchar(120) PRIMARY KEY,
            enabled boolean NOT NULL DEFAULT true,
            environments jsonb NOT NULL DEFAULT '[]',
            overrides jsonb NOT NULL DEFAULT '{}',
            updated_by uuid REFERENCES users(id),
            updated_at timestamptz NOT NULL DEFAULT now())""",
        """CREATE TABLE IF NOT EXISTS detection_runs (
            rule_id varchar(120) NOT NULL,
            environment_id uuid NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
            last_run_at timestamptz NOT NULL,
            last_status varchar(20) NOT NULL,
            last_reason text,
            PRIMARY KEY (rule_id, environment_id))""",
        """CREATE TABLE IF NOT EXISTS detection_states (
            fingerprint varchar(64) PRIMARY KEY,
            rule_id varchar(120) NOT NULL,
            environment_id uuid NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
            resource varchar(300) NOT NULL,
            namespace varchar(253),
            status varchar(20) NOT NULL DEFAULT 'pending',
            consecutive integer NOT NULL DEFAULT 0,
            clear_checks integer NOT NULL DEFAULT 0,
            first_seen timestamptz NOT NULL,
            last_seen timestamptz NOT NULL,
            incident_id uuid REFERENCES incidents(id) ON DELETE SET NULL)""",
        """CREATE TABLE IF NOT EXISTS maintenance_windows (
            id uuid PRIMARY KEY,
            environment_id uuid NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
            namespace varchar(253) NOT NULL DEFAULT '*',
            rule_id varchar(120) NOT NULL DEFAULT '*',
            starts_at timestamptz NOT NULL,
            ends_at timestamptz NOT NULL,
            reason text NOT NULL,
            created_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK (ends_at > starts_at))""",
    ):
        bind.exec_driver_sql(stmt)


def downgrade() -> None:
    raise RuntimeError("forward-only")
