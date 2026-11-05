"""Learning records, operator judgments, versioned datasets, and the model registry, artifacts, observations and
capability switches behind the ML features (docs/ML.md). Additive, so the previous build keeps working during a
rollout; nothing here changes how an investigation runs until a capability is switched on.

Revision ID: 0032
Revises: 0031
"""
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("ALTER TABLE role_bindings DROP CONSTRAINT role_bindings_role_check")
    bind.exec_driver_sql("""ALTER TABLE role_bindings ADD CONSTRAINT role_bindings_role_check CHECK (role IN
        ('viewer', 'investigator', 'operator', 'approver', 'terminal', 'knowledge_reviewer', 'knowledge_admin', 'index_operator', 'skill_publisher', 'ml_reviewer', 'ml_admin', 'admin'))""")

    # One row per investigation or incident: what it was about, what it retrieved, cited and ran, and what a
    # person later confirmed. Labels are empty until reviewed; a finished investigation is not a resolved incident.
    bind.exec_driver_sql("""CREATE TABLE learning_records (
        id uuid PRIMARY KEY,
        schema_version integer NOT NULL,
        kind varchar(20) NOT NULL CHECK (kind IN ('investigation', 'incident')),
        investigation_id uuid REFERENCES investigations(id) ON DELETE CASCADE,
        incident_id uuid REFERENCES incidents(id) ON DELETE SET NULL,
        environment varchar(100) NOT NULL,
        namespace varchar(253) NOT NULL DEFAULT '*',
        service varchar(200),
        resource varchar(300),
        realm varchar(20) NOT NULL DEFAULT 'operational' CHECK (realm IN ('operational', 'evaluation')),
        event_from timestamptz,
        event_to timestamptz,
        features jsonb NOT NULL DEFAULT '{}',
        retrieval jsonb NOT NULL DEFAULT '{}',
        skills jsonb NOT NULL DEFAULT '{}',
        outcome jsonb NOT NULL DEFAULT '{}',
        label jsonb NOT NULL DEFAULT '{}',
        label_state varchar(20) NOT NULL DEFAULT 'unreviewed' CHECK (label_state IN ('unreviewed', 'approved', 'rejected')),
        reviewed_by uuid REFERENCES users(id),
        reviewed_at timestamptz,
        versions jsonb NOT NULL DEFAULT '{}',
        fingerprint varchar(64) NOT NULL UNIQUE,
        content_hash varchar(64) NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz)""")
    bind.exec_driver_sql("CREATE INDEX learning_records_scope ON learning_records (environment, namespace, created_at DESC)")
    bind.exec_driver_sql("CREATE INDEX learning_records_incident ON learning_records (incident_id)")
    bind.exec_driver_sql("CREATE INDEX learning_records_label ON learning_records (label_state, kind)")
    bind.exec_driver_sql("CREATE INDEX learning_records_expiry ON learning_records (expires_at) WHERE expires_at IS NOT NULL")

    bind.exec_driver_sql("""CREATE TABLE learning_judgments (
        id uuid PRIMARY KEY,
        record_id uuid REFERENCES learning_records(id) ON DELETE CASCADE,
        context_id varchar(64) NOT NULL DEFAULT '',
        target_kind varchar(30) NOT NULL CHECK (target_kind IN ('passage', 'similar_incident', 'skill_recommendation', 'anomaly_observation')),
        target_id varchar(200) NOT NULL,
        verdict varchar(20) NOT NULL,
        note text NOT NULL DEFAULT '',
        environment varchar(100),
        namespace varchar(253),
        state varchar(20) NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'reviewed', 'dismissed')),
        created_by uuid NOT NULL REFERENCES users(id),
        reviewed_by uuid REFERENCES users(id),
        reviewed_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (created_by, target_kind, target_id, context_id))""")
    bind.exec_driver_sql("CREATE INDEX learning_judgments_target ON learning_judgments (target_kind, target_id)")

    bind.exec_driver_sql("""CREATE TABLE learning_datasets (
        id uuid PRIMARY KEY,
        name varchar(100) NOT NULL,
        version integer NOT NULL,
        purpose varchar(30) NOT NULL,
        realm varchar(20) NOT NULL DEFAULT 'operational',
        feature_schema varchar(40) NOT NULL,
        filters jsonb NOT NULL DEFAULT '{}',
        split jsonb NOT NULL DEFAULT '{}',
        counts jsonb NOT NULL DEFAULT '{}',
        checksum varchar(64) NOT NULL,
        state varchar(20) NOT NULL DEFAULT 'built' CHECK (state IN ('built', 'retired')),
        created_by uuid REFERENCES users(id),
        created_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz,
        UNIQUE (name, version))""")
    bind.exec_driver_sql("""CREATE TABLE learning_dataset_items (
        dataset_id uuid NOT NULL REFERENCES learning_datasets(id) ON DELETE CASCADE,
        record_id uuid NOT NULL REFERENCES learning_records(id) ON DELETE CASCADE,
        split varchar(10) NOT NULL CHECK (split IN ('train', 'eval')),
        group_key varchar(200) NOT NULL,
        PRIMARY KEY (dataset_id, record_id))""")

    # Trusted model artifacts: JSON only, addressed by their SHA-256, never pickles.
    bind.exec_driver_sql("""CREATE TABLE ml_artifacts (
        sha256 varchar(64) PRIMARY KEY,
        media_type varchar(80) NOT NULL,
        body bytea,
        uri text,
        bytes integer NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now())""")
    bind.exec_driver_sql("""CREATE TABLE ml_models (
        id uuid PRIMARY KEY,
        capability varchar(40) NOT NULL,
        cohort varchar(300) NOT NULL,
        algorithm varchar(40) NOT NULL,
        version integer NOT NULL,
        status varchar(20) NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate', 'shadow', 'champion', 'retired', 'rejected')),
        feature_schema varchar(40) NOT NULL,
        dataset_id uuid REFERENCES learning_datasets(id) ON DELETE SET NULL,
        dataset_version varchar(80),
        artifact_sha256 varchar(64) REFERENCES ml_artifacts(sha256),
        library_versions jsonb NOT NULL DEFAULT '{}',
        params jsonb NOT NULL DEFAULT '{}',
        metrics jsonb NOT NULL DEFAULT '{}',
        training jsonb NOT NULL DEFAULT '{}',
        provenance jsonb NOT NULL DEFAULT '{}',
        note text NOT NULL DEFAULT '',
        created_by uuid REFERENCES users(id),
        created_at timestamptz NOT NULL DEFAULT now(),
        promoted_by uuid REFERENCES users(id),
        promoted_at timestamptz,
        retired_at timestamptz,
        UNIQUE (capability, cohort, algorithm, version))""")
    bind.exec_driver_sql("CREATE UNIQUE INDEX ml_models_one_champion ON ml_models (capability, cohort) WHERE status = 'champion'")

    bind.exec_driver_sql("""CREATE TABLE ml_observations (
        id uuid PRIMARY KEY,
        capability varchar(40) NOT NULL,
        model_id uuid REFERENCES ml_models(id) ON DELETE SET NULL,
        algorithm varchar(40) NOT NULL,
        environment varchar(100) NOT NULL,
        namespace varchar(253) NOT NULL,
        service varchar(200) NOT NULL DEFAULT '',
        cohort varchar(300) NOT NULL,
        window_from timestamptz NOT NULL,
        window_to timestamptz NOT NULL,
        observed_at timestamptz NOT NULL DEFAULT now(),
        status varchar(30) NOT NULL,
        mode varchar(20) NOT NULL,
        score double precision,
        threshold double precision,
        unusual boolean NOT NULL DEFAULT false,
        deviations jsonb NOT NULL DEFAULT '[]',
        data_quality jsonb NOT NULL DEFAULT '{}',
        context jsonb NOT NULL DEFAULT '{}',
        feedback jsonb NOT NULL DEFAULT '{}',
        fingerprint varchar(64) NOT NULL UNIQUE)""")
    bind.exec_driver_sql("CREATE INDEX ml_observations_scope ON ml_observations (environment, namespace, observed_at DESC)")

    # disabled | shadow | enabled for each capability. The kill switch is mode=disabled, read on every use.
    bind.exec_driver_sql("""CREATE TABLE ml_capabilities (
        name varchar(40) PRIMARY KEY,
        mode varchar(20) NOT NULL CHECK (mode IN ('disabled', 'shadow', 'enabled')),
        reason text NOT NULL DEFAULT '',
        updated_by uuid REFERENCES users(id),
        updated_at timestamptz NOT NULL DEFAULT now())""")
    bind.exec_driver_sql("""INSERT INTO ml_capabilities (name, mode, reason) VALUES
        ('similar_incidents', 'enabled', 'read-only retrieval over approved incidents; default'),
        ('anomaly_detection', 'disabled', 'off until an operator enables shadow mode'),
        ('skill_recommendations', 'disabled', 'off until an operator enables shadow mode')""")


def downgrade() -> None:
    bind = op.get_bind()
    for t in ("ml_capabilities", "ml_observations", "ml_models", "ml_artifacts", "learning_dataset_items", "learning_datasets", "learning_judgments", "learning_records"):
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS {t}")
    bind.exec_driver_sql("DELETE FROM role_bindings WHERE role IN ('ml_reviewer', 'ml_admin')")
    bind.exec_driver_sql("ALTER TABLE role_bindings DROP CONSTRAINT role_bindings_role_check")
    bind.exec_driver_sql("""ALTER TABLE role_bindings ADD CONSTRAINT role_bindings_role_check CHECK (role IN
        ('viewer', 'investigator', 'operator', 'approver', 'terminal', 'knowledge_reviewer', 'knowledge_admin', 'index_operator', 'skill_publisher', 'admin'))""")
