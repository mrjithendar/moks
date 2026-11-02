"""Index generations know their mapping and when they were retired, and the worker records what it sees of
a search cluster. Additive: existing generations keep working as mapping version 1.

Revision ID: 0030
Revises: 0029
"""
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("ALTER TABLE embedding_indexes ADD COLUMN mapping_version integer NOT NULL DEFAULT 1, ADD COLUMN retired_at timestamptz")
    bind.exec_driver_sql("UPDATE embedding_indexes SET retired_at = now() WHERE status = 'retired'")
    bind.exec_driver_sql("""CREATE TABLE search_backend_status (
        backend varchar(20) PRIMARY KEY,
        checked_at timestamptz NOT NULL DEFAULT now(),
        state jsonb NOT NULL DEFAULT '{}')""")


def downgrade() -> None:
    raise RuntimeError("forward-only")
