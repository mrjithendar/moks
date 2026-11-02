"""Separate knowledge and skill permissions need no schema. This adds what backfill needs to stop one document
holding up the rest, what ingestion could not keep, retrieval feedback, and the record of skill review and of
what an approved proposal made. Additive, so the previous build keeps working during a rollout.

Revision ID: 0031
Revises: 0030
"""
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("ALTER TABLE role_bindings DROP CONSTRAINT role_bindings_role_check")
    bind.exec_driver_sql("""ALTER TABLE role_bindings ADD CONSTRAINT role_bindings_role_check CHECK (role IN
        ('viewer', 'investigator', 'operator', 'approver', 'terminal', 'knowledge_reviewer', 'knowledge_admin', 'index_operator', 'skill_publisher', 'admin'))""")
    bind.exec_driver_sql("""ALTER TABLE documents ADD COLUMN vector_attempts integer NOT NULL DEFAULT 0, ADD COLUMN vector_tried_at timestamptz, ADD COLUMN ingest_note text""")
    # A document with nothing to embed was recorded as missing vectors, and held the front of the backfill queue for good.
    bind.exec_driver_sql("""UPDATE documents d SET vector_state = 'not_applicable', vector_error = NULL
        WHERE d.status = 'active' AND d.vector_state IN ('missing', 'unknown') AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = d.id)""")
    bind.exec_driver_sql("ALTER TABLE skill_versions ADD COLUMN review_note text")
    bind.exec_driver_sql("ALTER TABLE skill_generations ADD COLUMN previous bigint")
    bind.exec_driver_sql("""UPDATE skill_generations g SET previous = (SELECT max(p.generation) FROM skill_generations p WHERE p.generation < g.generation)""")
    bind.exec_driver_sql("ALTER TABLE improvement_proposals ADD COLUMN draft_ref jsonb NOT NULL DEFAULT '{}'")
    bind.exec_driver_sql("""CREATE TABLE retrieval_feedback (
        id uuid PRIMARY KEY,
        query text NOT NULL,
        environment varchar(100),
        namespace varchar(253),
        document_id uuid,
        chunk_id uuid,
        verdict varchar(20) NOT NULL,
        note text NOT NULL DEFAULT '',
        context jsonb NOT NULL DEFAULT '{}',
        state varchar(20) NOT NULL DEFAULT 'open',
        created_by uuid NOT NULL REFERENCES users(id),
        reviewed_by uuid REFERENCES users(id),
        reviewed_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now())""")
    bind.exec_driver_sql("CREATE INDEX ix_retrieval_feedback_state ON retrieval_feedback (state, created_at DESC)")


def downgrade() -> None:
    raise RuntimeError("forward-only")
