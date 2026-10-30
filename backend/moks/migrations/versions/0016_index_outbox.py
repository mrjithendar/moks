"""Transactional indexing outbox and per-document revisions. Additive.

Revision ID: 0016
Revises: 0015
"""
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_revision bigint NOT NULL DEFAULT 0")
    conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN IF NOT EXISTS indexed_revision bigint NOT NULL DEFAULT 0")
    conn.exec_driver_sql("ALTER TABLE embedding_indexes ADD COLUMN IF NOT EXISTS build_watermark bigint")
    conn.exec_driver_sql("ALTER TABLE embedding_indexes ADD COLUMN IF NOT EXISTS build_job_id uuid")
    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS index_outbox (
            id bigserial PRIMARY KEY,
            document_id uuid NOT NULL,
            revision bigint NOT NULL,
            op varchar(10) NOT NULL CHECK (op IN ('upsert', 'delete')),
            status varchar(10) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'dead')),
            attempts integer NOT NULL DEFAULT 0,
            next_attempt_at timestamptz NOT NULL DEFAULT now(),
            error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            processed_at timestamptz,
            UNIQUE (document_id, revision, op)
        )""")
    conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_index_outbox_pending ON index_outbox (next_attempt_at) WHERE status = 'pending'")


def downgrade() -> None:
    raise RuntimeError("forward-only")
