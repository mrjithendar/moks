"""Safe execution: bound write targets, plan hash v2, executor leases and mutation checkpoints.

Plans still pending, approved or queued were hashed by the previous scheme and are expired rather
than re-hashed: their approvals did not cover the target identity. A plan caught mid-execution has
an unknown outcome and goes to reconciliation. Connectors that read their own cluster keep working
because their implicit write target becomes explicit.

Revision ID: 0004
Revises: 0003
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

STATUSES = "'pending','approved','rejected','expired','queued','executing','succeeded','failed','recovered','cancelled','reconciliation_required','reconciled'"


def upgrade() -> None:
    bind = op.get_bind()
    for stmt in (
        "ALTER TABLE action_plans DROP CONSTRAINT IF EXISTS action_plans_status_check",
        "ALTER TABLE action_plans ALTER COLUMN status TYPE varchar(30)",
        "ALTER TABLE audit_events ALTER COLUMN outcome TYPE varchar(30)",
        f"ALTER TABLE action_plans ADD CONSTRAINT action_plans_status_check CHECK (status IN ({STATUSES}))",
        "ALTER TABLE action_plans ADD COLUMN IF NOT EXISTS hash_version integer NOT NULL DEFAULT 1",
        "ALTER TABLE action_plans ADD COLUMN IF NOT EXISTS target_identity jsonb",
        "ALTER TABLE action_plans ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz",
        "ALTER TABLE action_plans ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz",
        "ALTER TABLE action_plans ADD COLUMN IF NOT EXISTS mutation_state varchar(20) NOT NULL DEFAULT 'none'",
        "ALTER TABLE action_plans ADD COLUMN IF NOT EXISTS checkpoint jsonb",
        "CREATE INDEX IF NOT EXISTS action_plans_leases ON action_plans (lease_expires_at) WHERE status = 'executing'",
        """UPDATE action_plans SET status = 'expired',
               result = coalesce(result, '{}'::jsonb) || '{"error": "invalidated by the safe-execution upgrade; create a new plan"}'::jsonb
           WHERE status IN ('pending','approved','queued')""",
        "UPDATE action_plans SET mutation_state = 'unknown' WHERE status = 'executing'",
        """UPDATE connectors SET config = config || '{"executor": {"mode": "in_cluster"}}'::jsonb
           WHERE type = 'kubernetes' AND coalesce(config->>'mode', 'in_cluster') = 'in_cluster' AND config->'executor' IS NULL""",
    ):
        bind.exec_driver_sql(stmt)


def downgrade() -> None:
    raise RuntimeError("forward-only")
