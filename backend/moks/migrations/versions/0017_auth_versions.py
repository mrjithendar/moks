"""Authorization versions, kept by triggers so no write path can forget them. Additive.

Revision ID: 0017
Revises: 0016
"""
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_version bigint NOT NULL DEFAULT 1")
    conn.exec_driver_sql("ALTER TABLE environments ADD COLUMN IF NOT EXISTS policy_version bigint NOT NULL DEFAULT 1")
    conn.exec_driver_sql("""
        CREATE OR REPLACE FUNCTION moks_bump_user_auth() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          UPDATE users SET auth_version = auth_version + 1 WHERE id = COALESCE(NEW.user_id, OLD.user_id);
          RETURN NULL;
        END $$""")
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS role_bindings_auth_version ON role_bindings")
    conn.exec_driver_sql("CREATE TRIGGER role_bindings_auth_version AFTER INSERT OR UPDATE OR DELETE ON role_bindings FOR EACH ROW EXECUTE FUNCTION moks_bump_user_auth()")
    conn.exec_driver_sql("""
        CREATE OR REPLACE FUNCTION moks_bump_user_active() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          NEW.auth_version := OLD.auth_version + 1;
          RETURN NEW;
        END $$""")
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS users_active_auth_version ON users")
    conn.exec_driver_sql("CREATE TRIGGER users_active_auth_version BEFORE UPDATE OF is_active ON users FOR EACH ROW WHEN (OLD.is_active IS DISTINCT FROM NEW.is_active) EXECUTE FUNCTION moks_bump_user_active()")
    conn.exec_driver_sql("""
        CREATE OR REPLACE FUNCTION moks_bump_env_policy() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          NEW.policy_version := OLD.policy_version + 1;
          RETURN NEW;
        END $$""")
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS environments_policy_version ON environments")
    conn.exec_driver_sql("""CREATE TRIGGER environments_policy_version BEFORE UPDATE OF allowed_namespaces, denied_namespaces ON environments FOR EACH ROW
        WHEN (OLD.allowed_namespaces IS DISTINCT FROM NEW.allowed_namespaces OR OLD.denied_namespaces IS DISTINCT FROM NEW.denied_namespaces)
        EXECUTE FUNCTION moks_bump_env_policy()""")


def downgrade() -> None:
    raise RuntimeError("forward-only")
