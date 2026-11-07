"""Scheduled secret sync (docs/SECRETS_SYNC.md): a schedule names mappings and an interval, and one approval of the
schedule lets the executor sync one-sided changes on each run. Approvals are insert-only, like plan approvals, and
bind the schedule's hash, so editing the schedule or one of its mappings needs a new approval.

Revision ID: 0034
Revises: 0033
"""
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("""CREATE TABLE secret_sync_schedules (
        id uuid PRIMARY KEY,
        environment_id uuid NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
        name varchar(100) NOT NULL,
        mapping_ids jsonb NOT NULL DEFAULT '[]',
        mapping_versions jsonb NOT NULL DEFAULT '{}',
        interval_minutes integer NOT NULL CHECK (interval_minutes BETWEEN 5 AND 10080),
        status varchar(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'active', 'paused', 'rejected', 'retired')),
        paused_reason text,
        schedule_hash varchar(64) NOT NULL DEFAULT '',
        version integer NOT NULL DEFAULT 1,
        requested_by uuid NOT NULL REFERENCES users(id),
        next_run_at timestamptz,
        last_run_at timestamptz,
        last_plan_id uuid REFERENCES secret_plans(id) ON DELETE SET NULL,
        last_outcome jsonb NOT NULL DEFAULT '{}',
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (environment_id, name))""")
    bind.exec_driver_sql("CREATE INDEX secret_sync_schedules_due ON secret_sync_schedules (next_run_at) WHERE status = 'active'")
    bind.exec_driver_sql("""CREATE TABLE secret_schedule_approvals (
        id uuid PRIMARY KEY,
        schedule_id uuid NOT NULL REFERENCES secret_sync_schedules(id) ON DELETE CASCADE,
        approver_id uuid NOT NULL REFERENCES users(id),
        decision varchar(10) NOT NULL CHECK (decision IN ('approve', 'reject')),
        schedule_hash varchar(64) NOT NULL,
        comment text NOT NULL DEFAULT '',
        created_at timestamptz NOT NULL DEFAULT now())""")


def downgrade() -> None:
    bind = op.get_bind()
    for t in ("secret_schedule_approvals", "secret_sync_schedules"):
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS {t}")
