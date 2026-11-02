"""A generation records which release produced it and what evaluation backed it. Additive.

Revision ID: 0028
Revises: 0027
"""
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql("ALTER TABLE skill_generations ADD COLUMN release_revision varchar(100), ADD COLUMN evaluation jsonb NOT NULL DEFAULT '{}'")


def downgrade() -> None:
    raise RuntimeError("forward-only")
