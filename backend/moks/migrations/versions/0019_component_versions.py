"""What each running component reports, so a mixed rollout is visible. Additive.

Revision ID: 0019
Revises: 0018
"""
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS component_versions (
            instance varchar(120) PRIMARY KEY,
            component varchar(20) NOT NULL,
            commit_sha varchar(64) NOT NULL,
            version varchar(30) NOT NULL,
            seen_at timestamptz NOT NULL DEFAULT now()
        )""")


def downgrade() -> None:
    raise RuntimeError("forward-only")
