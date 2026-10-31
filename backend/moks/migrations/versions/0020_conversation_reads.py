"""Per-person read state for conversations, so the inbox can show what is new. Additive.

People already in a conversation start with it read, or every old conversation would appear unread.

Revision ID: 0020
Revises: 0019
"""
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS conversation_reads (
            conversation_id uuid NOT NULL REFERENCES conversations(id),
            user_id uuid NOT NULL REFERENCES users(id),
            read_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (conversation_id, user_id)
        )""")
    bind.exec_driver_sql("""
        INSERT INTO conversation_reads (conversation_id, user_id)
        SELECT conversation_id, user_id FROM conversation_participants
        ON CONFLICT DO NOTHING""")


def downgrade() -> None:
    raise RuntimeError("forward-only")
