"""Backup run history, so the metric and the portal can tell when backups stop. Additive.

Revision ID: 0010
Revises: 0009
"""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS backup_runs (
            id uuid PRIMARY KEY,
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            status varchar(20) NOT NULL CHECK (status IN ('success', 'failed')),
            object_key text,
            size_bytes bigint,
            sha256 varchar(64),
            database_bytes bigint,
            tables jsonb,
            error text
        )""")


def downgrade() -> None:
    raise RuntimeError("forward-only")
