"""Secret sync between Kubernetes and AWS Secrets Manager (docs/SECRETS_SYNC.md): mappings with their verified
baseline, discovery inventory, reviewed plans with per-item state, and plan approvals. Additive; nothing runs
until MOKS_SECRETS_SYNC_ENABLED is set. No column holds a secret value: baselines and plan items keep version
identifiers and keyed digests only.

Revision ID: 0033
Revises: 0032
"""
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

ROLES_BEFORE = "'viewer', 'investigator', 'operator', 'approver', 'terminal', 'knowledge_reviewer', 'knowledge_admin', 'index_operator', 'skill_publisher', 'ml_reviewer', 'ml_admin', 'admin'"
ROLES_AFTER = ROLES_BEFORE + ", 'secrets_viewer', 'secrets_operator', 'secrets_approver'"


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("ALTER TABLE role_bindings DROP CONSTRAINT role_bindings_role_check")
    bind.exec_driver_sql(f"ALTER TABLE role_bindings ADD CONSTRAINT role_bindings_role_check CHECK (role IN ({ROLES_AFTER}))")

    bind.exec_driver_sql("""CREATE TABLE secret_mappings (
        id uuid PRIMARY KEY,
        environment_id uuid NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
        kube_connector_id uuid NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
        cluster_uid varchar(64) NOT NULL,
        cluster_name varchar(100) NOT NULL DEFAULT '',
        namespace varchar(253) NOT NULL,
        secret_name varchar(253) NOT NULL,
        aws_connector_id uuid NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
        aws_account varchar(12) NOT NULL,
        aws_region varchar(30) NOT NULL,
        aws_secret_name varchar(512) NOT NULL,
        aws_secret_arn text,
        payload_format varchar(20) NOT NULL CHECK (payload_format IN ('moks-k8s-v1', 'json', 'text', 'binary')),
        key_mapping jsonb NOT NULL DEFAULT '{}',
        k8s_type varchar(100) NOT NULL DEFAULT 'Opaque',
        direction varchar(10) NOT NULL CHECK (direction IN ('export', 'import', 'both')),
        expected_owner jsonb NOT NULL DEFAULT '{}',
        management_policy varchar(20) NOT NULL DEFAULT 'refuse_managed' CHECK (management_policy IN ('refuse_managed', 'override')),
        status varchar(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
        last_status varchar(30),
        last_compared_at timestamptz,
        version integer NOT NULL DEFAULT 1,
        baseline_seq integer NOT NULL DEFAULT 0,
        baseline jsonb,
        lease_owner uuid,
        lease_expires_at timestamptz,
        created_by uuid REFERENCES users(id),
        updated_by uuid REFERENCES users(id),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now())""")
    # One active mapping per Kubernetes Secret and per AWS secret: two mappings writing one secret would fight.
    bind.exec_driver_sql("CREATE UNIQUE INDEX secret_mappings_kube ON secret_mappings (cluster_uid, namespace, secret_name) WHERE status = 'active'")
    bind.exec_driver_sql("CREATE UNIQUE INDEX secret_mappings_aws ON secret_mappings (aws_account, aws_region, aws_secret_name) WHERE status = 'active'")
    bind.exec_driver_sql("CREATE INDEX secret_mappings_env ON secret_mappings (environment_id, namespace)")

    bind.exec_driver_sql("""CREATE TABLE secret_inventory (
        id uuid PRIMARY KEY,
        environment_id uuid NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
        side varchar(12) NOT NULL CHECK (side IN ('kubernetes', 'aws')),
        connector_id uuid NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
        scope_key varchar(300) NOT NULL,
        cluster_uid varchar(64),
        namespace varchar(253),
        name varchar(512) NOT NULL,
        arn text,
        type varchar(100),
        key_names varchar[],
        owner jsonb NOT NULL DEFAULT '{}',
        syncable boolean NOT NULL DEFAULT true,
        excluded text,
        last_modified varchar(40),
        version varchar(100),
        tags jsonb NOT NULL DEFAULT '{}',
        extra jsonb NOT NULL DEFAULT '{}',
        job_id uuid,
        discovered_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (environment_id, side, connector_id, scope_key, name))""")
    bind.exec_driver_sql("CREATE INDEX secret_inventory_env ON secret_inventory (environment_id, side, namespace)")
    bind.exec_driver_sql("""CREATE TABLE secret_inventory_scopes (
        id uuid PRIMARY KEY,
        environment_id uuid NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
        side varchar(12) NOT NULL,
        connector_id uuid NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
        scope_key varchar(300) NOT NULL,
        cluster_uid varchar(64),
        complete boolean NOT NULL DEFAULT true,
        count integer NOT NULL DEFAULT 0,
        error text,
        job_id uuid,
        discovered_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (environment_id, side, connector_id, scope_key))""")

    bind.exec_driver_sql("""CREATE TABLE secret_plans (
        id uuid PRIMARY KEY,
        environment_id uuid NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
        kind varchar(12) NOT NULL CHECK (kind IN ('sync', 'rollback', 'delete')),
        status varchar(20) NOT NULL DEFAULT 'comparing' CHECK (status IN ('comparing', 'compare_failed', 'draft', 'pending', 'approved', 'rejected', 'queued',
                                                                          'executing', 'succeeded', 'partial', 'failed', 'cancelled', 'expired')),
        reason text NOT NULL DEFAULT '',
        requested_by uuid NOT NULL REFERENCES users(id),
        executed_by uuid REFERENCES users(id),
        plan_hash varchar(64) NOT NULL DEFAULT '',
        hash_version integer NOT NULL DEFAULT 1,
        requires_approval boolean NOT NULL DEFAULT true,
        expires_at timestamptz,
        version integer NOT NULL DEFAULT 1,
        request jsonb NOT NULL DEFAULT '{}',
        summary jsonb NOT NULL DEFAULT '{}',
        error text,
        compare_job_id uuid,
        execute_job_id uuid,
        source_plan_id uuid REFERENCES secret_plans(id) ON DELETE SET NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        submitted_at timestamptz,
        queued_at timestamptz,
        finished_at timestamptz)""")
    bind.exec_driver_sql("CREATE INDEX secret_plans_env ON secret_plans (environment_id, created_at DESC)")
    bind.exec_driver_sql("""CREATE TABLE secret_plan_items (
        id uuid PRIMARY KEY,
        plan_id uuid NOT NULL REFERENCES secret_plans(id) ON DELETE CASCADE,
        mapping_id uuid NOT NULL REFERENCES secret_mappings(id) ON DELETE CASCADE,
        position integer NOT NULL DEFAULT 0,
        included boolean NOT NULL DEFAULT true,
        mapping_version integer NOT NULL DEFAULT 0,
        baseline_seq integer NOT NULL DEFAULT 0,
        classification varchar(30),
        first_sync boolean NOT NULL DEFAULT false,
        proposal varchar(20),
        resolution varchar(20) CHECK (resolution IN ('kubernetes', 'aws', 'edited')),
        key_choices jsonb NOT NULL DEFAULT '{}',
        rollback_version varchar(100),
        targets varchar[] NOT NULL DEFAULT '{}',
        actions jsonb NOT NULL DEFAULT '{}',
        kube_state jsonb NOT NULL DEFAULT '{}',
        aws_state jsonb NOT NULL DEFAULT '{}',
        source_state jsonb NOT NULL DEFAULT '{}',
        baseline jsonb,
        expected jsonb NOT NULL DEFAULT '{}',
        diff jsonb NOT NULL DEFAULT '{}',
        warnings jsonb NOT NULL DEFAULT '[]',
        impact jsonb NOT NULL DEFAULT '[]',
        blocked text,
        status varchar(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'stale', 'skipped', 'conflict')),
        write_state jsonb NOT NULL DEFAULT '{}',
        idempotency_token uuid NOT NULL,
        attempts integer NOT NULL DEFAULT 0,
        result jsonb NOT NULL DEFAULT '{}',
        error text,
        updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (plan_id, mapping_id))""")
    bind.exec_driver_sql("CREATE INDEX secret_plan_items_mapping ON secret_plan_items (mapping_id, updated_at DESC)")
    bind.exec_driver_sql("""CREATE TABLE secret_plan_approvals (
        id uuid PRIMARY KEY,
        plan_id uuid NOT NULL REFERENCES secret_plans(id) ON DELETE CASCADE,
        approver_id uuid NOT NULL REFERENCES users(id),
        decision varchar(10) NOT NULL CHECK (decision IN ('approve', 'reject')),
        plan_hash varchar(64) NOT NULL,
        comment text NOT NULL DEFAULT '',
        created_at timestamptz NOT NULL DEFAULT now())""")


def downgrade() -> None:
    bind = op.get_bind()
    for t in ("secret_plan_approvals", "secret_plan_items", "secret_plans", "secret_inventory_scopes", "secret_inventory", "secret_mappings"):
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS {t}")
    bind.exec_driver_sql("DELETE FROM role_bindings WHERE role IN ('secrets_viewer', 'secrets_operator', 'secrets_approver')")
    bind.exec_driver_sql("ALTER TABLE role_bindings DROP CONSTRAINT role_bindings_role_check")
    bind.exec_driver_sql(f"ALTER TABLE role_bindings ADD CONSTRAINT role_bindings_role_check CHECK (role IN ({ROLES_BEFORE}))")
