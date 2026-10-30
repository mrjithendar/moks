"""Job classes, so each kind of work has its own capacity. Additive.

Revision ID: 0015
Revises: 0014
"""
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS job_class varchar(20) NOT NULL DEFAULT 'maintenance'")
    conn.exec_driver_sql("""UPDATE jobs SET job_class = CASE
        WHEN type IN ('detection_tick', 'alert_poll') THEN 'detection'
        WHEN type IN ('knowledge_sync', 'knowledge_reindex') THEN 'knowledge'
        ELSE 'maintenance' END WHERE status IN ('queued', 'running')""")
    conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_jobs_class_queue ON jobs (job_class, run_after) WHERE status = 'queued'")


def downgrade() -> None:
    raise RuntimeError("forward-only")
