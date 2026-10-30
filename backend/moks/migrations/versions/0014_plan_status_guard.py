"""Limits the status changes the API's database role can make to an action plan. Additive.

The trigger acts only for the role moks_api, so installs on a single shared user are unaffected
until they move to separate roles (docs/SECRET_SPLIT.md).

Revision ID: 0014
Revises: 0013
"""
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("""
        CREATE OR REPLACE FUNCTION moks_plan_status_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF current_user = 'moks_api' AND NEW.status IS DISTINCT FROM OLD.status AND NOT (
               (OLD.status = 'pending' AND NEW.status IN ('approved', 'rejected', 'expired', 'cancelled', 'queued'))
            OR (OLD.status = 'approved' AND NEW.status IN ('expired', 'cancelled', 'queued'))
            OR (OLD.status = 'queued' AND NEW.status = 'cancelled')
            OR (OLD.status = 'reconciliation_required' AND NEW.status = 'reconciled')
          ) THEN
            RAISE EXCEPTION 'the API role may not move a plan from % to %', OLD.status, NEW.status USING ERRCODE = '42501';
          END IF;
          RETURN NEW;
        END $$""")
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS action_plans_status_guard ON action_plans")
    conn.exec_driver_sql("CREATE TRIGGER action_plans_status_guard BEFORE UPDATE ON action_plans FOR EACH ROW EXECUTE FUNCTION moks_plan_status_guard()")


def downgrade() -> None:
    raise RuntimeError("forward-only")
