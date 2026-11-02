"""Document lifecycle and trust, and reviewed proposals from incidents. Additive: documents stay searchable,
but nothing is marked reviewed, because nobody has reviewed it.

Revision ID: 0029
Revises: 0028
"""
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("""ALTER TABLE documents
        ADD COLUMN lifecycle varchar(20) NOT NULL DEFAULT 'active', ADD COLUMN trust varchar(20) NOT NULL DEFAULT 'unreviewed',
        ADD COLUMN reviewed_by uuid REFERENCES users(id), ADD COLUMN reviewed_at timestamptz, ADD COLUMN review_due_at timestamptz,
        ADD COLUMN superseded_by_id uuid REFERENCES documents(id) ON DELETE SET NULL,
        ADD CONSTRAINT documents_lifecycle_check CHECK (lifecycle IN ('draft', 'quarantined', 'active', 'archived', 'deleted'))""")
    bind.exec_driver_sql("ALTER TABLE knowledge_sources ADD COLUMN review_required boolean NOT NULL DEFAULT false")
    bind.exec_driver_sql("""CREATE TABLE improvement_proposals (
        id uuid PRIMARY KEY,
        investigation_id uuid NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
        kind varchar(20) NOT NULL CHECK (kind IN ('runbook', 'skill')),
        title varchar(200) NOT NULL,
        body text NOT NULL,
        evidence jsonb NOT NULL DEFAULT '[]',
        regression_fixture jsonb NOT NULL DEFAULT '{}',
        owner varchar(200) NOT NULL,
        version integer NOT NULL DEFAULT 1,
        state varchar(20) NOT NULL DEFAULT 'draft' CHECK (state IN ('draft', 'approved', 'rejected')),
        created_by uuid NOT NULL REFERENCES users(id),
        reviewed_by uuid REFERENCES users(id),
        reviewed_at timestamptz,
        review_note text,
        created_at timestamptz NOT NULL DEFAULT now())""")


def downgrade() -> None:
    raise RuntimeError("forward-only")
