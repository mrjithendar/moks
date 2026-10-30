"""Saved metric queries with ownership and sharing.

Revision ID: 0006
Revises: 0005
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS saved_queries (
            id uuid PRIMARY KEY,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name varchar(200) NOT NULL,
            description text NOT NULL DEFAULT '',
            language varchar(20) NOT NULL DEFAULT 'promql' CHECK (language IN ('promql', 'cloudwatch')),
            query text NOT NULL,
            environments jsonb NOT NULL DEFAULT '[]',
            shared boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )""")


def downgrade() -> None:
    raise RuntimeError("forward-only")
