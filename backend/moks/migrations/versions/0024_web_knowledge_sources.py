"""Knowledge sources can be web pages. Additive: every row that was valid stays valid.

Revision ID: 0024
Revises: 0023
"""
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql("ALTER TABLE knowledge_sources DROP CONSTRAINT IF EXISTS knowledge_sources_type_check")
    op.get_bind().exec_driver_sql("ALTER TABLE knowledge_sources ADD CONSTRAINT knowledge_sources_type_check CHECK (type IN ('github','upload','incident','web'))")


def downgrade() -> None:
    raise RuntimeError("forward-only")
