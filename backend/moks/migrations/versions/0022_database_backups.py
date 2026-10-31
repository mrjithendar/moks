"""Database backups: what was taken, where it is, what is inside, and their schedules. Additive.

Revision ID: 0022
Revises: 0021
"""
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS database_backups (
            id uuid PRIMARY KEY,
            environment_id uuid NOT NULL REFERENCES environments(id),
            namespace varchar(253) NOT NULL,
            workload varchar(253) NOT NULL,
            workload_kind varchar(40) NOT NULL,
            engine varchar(20) NOT NULL,
            status varchar(20) NOT NULL,
            trigger varchar(20) NOT NULL DEFAULT 'manual',
            plan_id uuid REFERENCES action_plans(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz,
            finished_at timestamptz,
            object_key text,
            size_bytes bigint,
            dump_bytes bigint,
            sha256 varchar(64),
            image text,
            pod varchar(253),
            inventory jsonb,
            error text
        )""")
    op.get_bind().exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_database_backups_target ON database_backups (environment_id, namespace, workload, created_at DESC)")
    op.get_bind().exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_database_backups_open ON database_backups (created_at) WHERE status IN ('queued', 'running')")
    op.get_bind().exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS database_backup_schedules (
            id uuid PRIMARY KEY,
            environment_id uuid NOT NULL REFERENCES environments(id),
            namespace varchar(253) NOT NULL,
            workload varchar(253) NOT NULL,
            workload_kind varchar(40) NOT NULL,
            engine varchar(20) NOT NULL,
            interval_hours integer NOT NULL,
            paused boolean NOT NULL DEFAULT false,
            next_run_at timestamptz NOT NULL,
            plan_id uuid REFERENCES action_plans(id) ON DELETE SET NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (environment_id, namespace, workload)
        )""")


def downgrade() -> None:
    raise RuntimeError("forward-only")
