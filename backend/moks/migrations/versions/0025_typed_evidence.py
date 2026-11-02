"""Evidence has a type, so a failed call can no longer pass for an observation. Additive: earlier reports keep their text.

Revision ID: 0025
Revises: 0024
"""
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("ALTER TABLE evidence ADD COLUMN kind varchar(20) NOT NULL DEFAULT 'observation'")
    bind.exec_driver_sql("""UPDATE evidence SET kind = CASE WHEN NOT ok THEN 'tool_error' WHEN tool IN ('kb_search', 'git_read_file') THEN 'documentation'
                            WHEN truncated THEN 'incomplete' ELSE 'observation' END""")


def downgrade() -> None:
    raise RuntimeError("forward-only")
