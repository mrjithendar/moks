"""Administrators can switch individual skills off.

Revision ID: 0008
Revises: 0007
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS skill_policies (
            name varchar(120) PRIMARY KEY,
            enabled boolean NOT NULL DEFAULT true,
            updated_by uuid REFERENCES users(id),
            updated_at timestamptz NOT NULL DEFAULT now()
        )""")


def downgrade() -> None:
    raise RuntimeError("forward-only")
