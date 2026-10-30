"""Reserve LLM spend before each call, so concurrent calls cannot overrun a budget. Additive.

Revision ID: 0018
Revises: 0017
"""
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("ALTER TABLE llm_configs ADD COLUMN IF NOT EXISTS max_concurrency integer NOT NULL DEFAULT 4")
    conn.exec_driver_sql("ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS estimated_usd numeric(12,6)")
    conn.exec_driver_sql("ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS user_id uuid")
    conn.exec_driver_sql("ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS purpose varchar(30)")
    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS llm_reservations (
            id uuid PRIMARY KEY,
            llm_config_id uuid NOT NULL,
            user_id uuid,
            investigation_id uuid,
            purpose varchar(30) NOT NULL,
            estimated_usd numeric(12,6) NOT NULL,
            status varchar(12) NOT NULL DEFAULT 'held' CHECK (status IN ('held', 'settled', 'released', 'uncertain')),
            expires_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            settled_at timestamptz
        )""")
    conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_llm_reservations_open ON llm_reservations (llm_config_id, created_at) WHERE status IN ('held', 'uncertain')")


def downgrade() -> None:
    raise RuntimeError("forward-only")
