"""More environment kinds (generic Kubernetes) and a default environment for auto-scoped investigations.

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for stmt in (
        "ALTER TABLE environments DROP CONSTRAINT IF EXISTS environments_kind_check",
        "ALTER TABLE environments ADD CONSTRAINT environments_kind_check CHECK (kind IN ('minikube','eks','kubernetes'))",
        "ALTER TABLE environments ADD COLUMN IF NOT EXISTS is_default boolean NOT NULL DEFAULT false",
    ):
        bind.exec_driver_sql(stmt)


def downgrade() -> None:
    raise RuntimeError("forward-only")
