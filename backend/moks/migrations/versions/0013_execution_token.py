"""A token per execution claim, so a worker that lost its lease cannot write. Additive.

Revision ID: 0013
Revises: 0012
"""
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql("ALTER TABLE action_plans ADD COLUMN IF NOT EXISTS execution_token uuid")


def downgrade() -> None:
    raise RuntimeError("forward-only")
