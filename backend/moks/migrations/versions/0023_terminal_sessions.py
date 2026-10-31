"""Terminal sessions: who opened a shell, with which scope, and how it ended. Additive.

Revision ID: 0023
Revises: 0022
"""
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Widens the list of roles: every row that was valid stays valid.
    op.get_bind().exec_driver_sql("ALTER TABLE role_bindings DROP CONSTRAINT IF EXISTS role_bindings_role_check")
    op.get_bind().exec_driver_sql("ALTER TABLE role_bindings ADD CONSTRAINT role_bindings_role_check CHECK (role IN ('viewer', 'investigator', 'operator', 'approver', 'terminal', 'admin'))")
    op.get_bind().exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS terminal_sessions (
            id uuid PRIMARY KEY,
            owner_id uuid NOT NULL REFERENCES users(id),
            login_session varchar(64) NOT NULL,
            environment varchar(100) NOT NULL,
            profile varchar(100) NOT NULL,
            aws_account varchar(12) NOT NULL,
            role_arn text NOT NULL,
            region varchar(30) NOT NULL,
            cluster varchar(200),
            namespaces varchar[] NOT NULL DEFAULT '{}',
            purpose text NOT NULL,
            status varchar(20) NOT NULL DEFAULT 'starting',
            end_reason varchar(30),
            error text,
            image text NOT NULL,
            pod_name varchar(253) NOT NULL,
            pod_namespace varchar(253) NOT NULL,
            pod_deleted_at timestamptz,
            aws_identity text,
            kube_identity text,
            role_session_name varchar(64) NOT NULL,
            idle_minutes integer NOT NULL,
            lifetime_minutes integer NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            ready_at timestamptz,
            expires_at timestamptz NOT NULL,
            credential_expires_at timestamptz,
            last_input_at timestamptz,
            attachment uuid,
            attach_count integer NOT NULL DEFAULT 0,
            ended_at timestamptz
        )""")
    op.get_bind().exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_terminal_sessions_owner ON terminal_sessions (owner_id, created_at DESC)")
    op.get_bind().exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_terminal_sessions_open ON terminal_sessions (status) WHERE status IN ('starting', 'ready')")
    op.get_bind().exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_terminal_sessions_cleanup ON terminal_sessions (ended_at) WHERE pod_deleted_at IS NULL")


def downgrade() -> None:
    raise RuntimeError("forward-only")
