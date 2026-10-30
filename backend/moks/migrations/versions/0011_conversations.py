"""Conversations: an investigation is a thread of turns that several people can join.

Additive. Existing investigations are carried over: a follow-up joins its root's conversation, and
each gets its question and answer as messages. A conversation keeps the id of its first investigation.

Revision ID: 0011
Revises: 0010
"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for stmt in (
        """CREATE TABLE IF NOT EXISTS conversations (
            id uuid PRIMARY KEY,
            title text NOT NULL,
            environment_id uuid NOT NULL REFERENCES environments(id),
            namespace varchar(253) NOT NULL,
            created_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            deleted_at timestamptz,
            deleted_by uuid REFERENCES users(id))""",
        """CREATE TABLE IF NOT EXISTS conversation_participants (
            conversation_id uuid NOT NULL REFERENCES conversations(id),
            user_id uuid NOT NULL REFERENCES users(id),
            added_by uuid REFERENCES users(id),
            joined_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (conversation_id, user_id))""",
        """CREATE TABLE IF NOT EXISTS conversation_messages (
            id uuid PRIMARY KEY,
            conversation_id uuid NOT NULL REFERENCES conversations(id),
            author_id uuid REFERENCES users(id),
            role varchar(20) NOT NULL CHECK (role IN ('user', 'assistant')),
            content text NOT NULL,
            investigation_id uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            deleted_at timestamptz,
            deleted_by uuid REFERENCES users(id))""",
        "CREATE INDEX IF NOT EXISTS conversation_messages_thread ON conversation_messages (conversation_id, created_at)",
        "ALTER TABLE investigations ADD COLUMN IF NOT EXISTS conversation_id uuid",
        "CREATE INDEX IF NOT EXISTS investigations_conversation ON investigations (conversation_id)",
        """WITH RECURSIVE tree AS (
               SELECT id, id AS root FROM investigations WHERE parent_id IS NULL
               UNION ALL SELECT i.id, t.root FROM investigations i JOIN tree t ON i.parent_id = t.id)
           UPDATE investigations i SET conversation_id = tree.root FROM tree WHERE i.id = tree.id AND i.conversation_id IS NULL""",
        """INSERT INTO conversations (id, title, environment_id, namespace, created_by, created_at, updated_at)
           SELECT i.id, left(i.question, 200), i.environment_id, i.namespace, i.created_by, i.created_at, i.created_at
           FROM investigations i WHERE i.id = i.conversation_id ON CONFLICT DO NOTHING""",
        """INSERT INTO conversation_participants (conversation_id, user_id, joined_at)
           SELECT conversation_id, created_by, min(created_at) FROM investigations WHERE conversation_id IS NOT NULL
           GROUP BY 1, 2 ON CONFLICT DO NOTHING""",
        """INSERT INTO conversation_messages (id, conversation_id, author_id, role, content, investigation_id, created_at)
           SELECT gen_random_uuid(), conversation_id, created_by, 'user', question, id, created_at FROM investigations WHERE conversation_id IS NOT NULL""",
        """INSERT INTO conversation_messages (id, conversation_id, role, content, investigation_id, created_at)
           SELECT gen_random_uuid(), conversation_id, 'assistant', coalesce(report->>'summary', error, 'No report was produced.'), id, coalesce(finished_at, created_at)
           FROM investigations WHERE conversation_id IS NOT NULL AND status IN ('completed', 'failed', 'cancelled')""",
    ):
        bind.exec_driver_sql(stmt)


def downgrade() -> None:
    raise RuntimeError("forward-only")
