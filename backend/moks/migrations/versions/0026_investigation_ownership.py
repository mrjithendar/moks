"""Investigations get a per-claim token and a lease, and a checkpoint a later claim can resume from. Additive.

Revision ID: 0026
Revises: 0025
"""
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("ALTER TABLE investigations ADD COLUMN execution_token uuid, ADD COLUMN lease_expires_at timestamptz, ADD COLUMN attempts integer NOT NULL DEFAULT 0")
    bind.exec_driver_sql("""CREATE TABLE investigation_checkpoints (
        investigation_id uuid PRIMARY KEY REFERENCES investigations(id) ON DELETE CASCADE,
        seq integer NOT NULL DEFAULT 0,
        node varchar(30) NOT NULL,
        next_node varchar(30) NOT NULL,
        attempt integer NOT NULL DEFAULT 1,
        state jsonb NOT NULL DEFAULT '{}',
        updated_at timestamptz NOT NULL DEFAULT now())""")
    bind.exec_driver_sql("CREATE INDEX ix_investigations_lease ON investigations (lease_expires_at) WHERE status = 'running'")


def downgrade() -> None:
    raise RuntimeError("forward-only")
