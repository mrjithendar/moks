"""Cloudflare R2 settings for cluster backups: references to credentials, never values. Additive.

Revision ID: 0012
Revises: 0011
"""
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS backup_stores (
            id uuid PRIMARY KEY,
            name varchar(40) NOT NULL UNIQUE,
            account_id varchar(32) NOT NULL,
            bucket varchar(63) NOT NULL,
            prefix text NOT NULL DEFAULT 'clusters',
            jurisdiction varchar(10) NOT NULL DEFAULT 'default',
            credentials jsonb NOT NULL DEFAULT '{}',
            updated_by uuid REFERENCES users(id),
            updated_at timestamptz NOT NULL DEFAULT now(),
            last_test_at timestamptz,
            last_test_ok boolean,
            last_test_error text
        )""")


def downgrade() -> None:
    raise RuntimeError("forward-only")
