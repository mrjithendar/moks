"""Recoverable embedding index lifecycle: one build at a time, physical cleanup after commit.

Revision ID: 0005
Revises: 0004
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for stmt in (
        "ALTER TABLE embedding_indexes ADD COLUMN IF NOT EXISTS cleanup_pending boolean NOT NULL DEFAULT false",
        "ALTER TABLE embedding_indexes ADD COLUMN IF NOT EXISTS error text",
        "ALTER TABLE embedding_indexes ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz",
        "UPDATE embedding_indexes SET status = 'failed', error = 'interrupted', cleanup_pending = true WHERE status = 'building'",
        "CREATE UNIQUE INDEX IF NOT EXISTS embedding_indexes_one_building ON embedding_indexes (backend) WHERE status = 'building'",
    ):
        bind.exec_driver_sql(stmt)


def downgrade() -> None:
    raise RuntimeError("forward-only")
