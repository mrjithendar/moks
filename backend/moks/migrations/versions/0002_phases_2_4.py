"""Phases 2-4: incidents, knowledge (RAG), background jobs, action plans and approvals,
diagnostic sessions, alert events, evaluation runs, and the automation principal.

pgvector is optional: when the extension is available the chunk_embeddings table gets a vector
column; otherwise the Postgres retrieval backend is keyword-only (OpenSearch provides vectors).

Revision ID: 0002
Revises: 0001
"""
from alembic import op

from moks.migrations import split_sql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

AUTOMATION_USER_ID = "00000000-0000-4000-8000-00000000a070"


def upgrade() -> None:
    bind = op.get_bind()
    for statement in split_sql(SCHEMA):
        bind.exec_driver_sql(statement)
    has_vector = bind.exec_driver_sql("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'").first()
    if has_vector:
        try:
            bind.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
            bind.exec_driver_sql("ALTER TABLE chunk_embeddings ADD COLUMN embedding vector")
        except Exception:  # extension present but not creatable by this role: stay keyword-only
            pass


SCHEMA = """
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_auth_provider_check;
ALTER TABLE users ADD CONSTRAINT users_auth_provider_check CHECK (auth_provider IN ('oidc','local','dev','system'));
INSERT INTO users (id, email, display_name, auth_provider, is_active)
  VALUES ('00000000-0000-4000-8000-00000000a070', 'automation@moks.system', 'MOKS automation', 'system', true)
  ON CONFLICT (email) DO NOTHING;

ALTER TABLE environments ADD COLUMN auto_investigate boolean NOT NULL DEFAULT false;
ALTER TABLE environments ADD COLUMN alert_cooldown_minutes integer NOT NULL DEFAULT 30 CHECK (alert_cooldown_minutes BETWEEN 1 AND 1440);
-- Remediation policy per environment (Phase 3)
ALTER TABLE environments ADD COLUMN max_replicas integer NOT NULL DEFAULT 10 CHECK (max_replicas BETWEEN 1 AND 1000);
ALTER TABLE environments ADD COLUMN allow_scale_to_zero boolean NOT NULL DEFAULT false;
ALTER TABLE environments ADD COLUMN plan_ttl_minutes integer NOT NULL DEFAULT 60 CHECK (plan_ttl_minutes BETWEEN 5 AND 1440);

-- ---- Incidents ------------------------------------------------------------------------------
CREATE TABLE incidents (
    id uuid PRIMARY KEY,
    title varchar(300) NOT NULL,
    severity varchar(10) NOT NULL DEFAULT 'sev3' CHECK (severity IN ('sev1','sev2','sev3','sev4')),
    status varchar(20) NOT NULL DEFAULT 'open' CHECK (status IN ('open','mitigated','resolved','closed')),
    environment_id uuid NOT NULL REFERENCES environments(id),
    namespace varchar(253) NOT NULL DEFAULT '*',
    summary text NOT NULL DEFAULT '',
    hypotheses jsonb NOT NULL DEFAULT '[]',
    source varchar(20) NOT NULL DEFAULT 'manual',
    fingerprint varchar(64),
    commander_id uuid REFERENCES users(id),
    created_by uuid NOT NULL REFERENCES users(id),
    postmortem jsonb,
    postmortem_status varchar(20) NOT NULL DEFAULT 'none' CHECK (postmortem_status IN ('none','draft','approved')),
    postmortem_approved_by uuid REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz
);
CREATE INDEX incidents_env ON incidents (environment_id, created_at DESC);
CREATE INDEX incidents_open_fp ON incidents (fingerprint) WHERE status IN ('open','mitigated');

CREATE TABLE incident_events (
    id bigserial PRIMARY KEY,
    incident_id uuid NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    ts timestamptz NOT NULL DEFAULT now(),
    kind varchar(30) NOT NULL,
    actor_id uuid REFERENCES users(id),
    payload jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX incident_events_inc ON incident_events (incident_id, ts);

ALTER TABLE investigations ADD COLUMN incident_id uuid REFERENCES incidents(id) ON DELETE SET NULL;
ALTER TABLE investigations ADD COLUMN trigger varchar(20) NOT NULL DEFAULT 'user';

-- ---- Background jobs (ingestion, reindex, alert polling, eval) --------------------------------
CREATE TABLE jobs (
    id uuid PRIMARY KEY,
    type varchar(40) NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}',
    status varchar(20) NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
    dedupe_key varchar(200),
    attempts integer NOT NULL DEFAULT 0,
    run_after timestamptz NOT NULL DEFAULT now(),
    result jsonb,
    error text,
    created_by uuid REFERENCES users(id),
    worker_id varchar(100),
    heartbeat_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz
);
CREATE INDEX jobs_queue ON jobs (run_after) WHERE status = 'queued';
CREATE UNIQUE INDEX jobs_dedupe ON jobs (dedupe_key) WHERE status IN ('queued','running') AND dedupe_key IS NOT NULL;

-- ---- Knowledge / RAG ------------------------------------------------------------------------
CREATE TABLE knowledge_sources (
    id uuid PRIMARY KEY,
    name varchar(100) NOT NULL UNIQUE,
    type varchar(20) NOT NULL CHECK (type IN ('github','upload','incident')),
    config jsonb NOT NULL DEFAULT '{}',
    credential_ref jsonb,
    -- Access scope: who may retrieve from this source (environment and namespace patterns).
    scope_environment varchar(100) NOT NULL DEFAULT '*',
    scope_namespace varchar(253) NOT NULL DEFAULT '*',
    service varchar(100),
    owner varchar(200),
    enabled boolean NOT NULL DEFAULT true,
    revision varchar(100),
    last_sync_at timestamptz,
    last_sync_status varchar(20),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE documents (
    id uuid PRIMARY KEY,
    source_id uuid NOT NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE,
    path varchar(1000) NOT NULL,
    title varchar(500) NOT NULL DEFAULT '',
    kind varchar(30) NOT NULL DEFAULT 'text',
    revision varchar(100),
    url text,
    content_hash varchar(64) NOT NULL,
    service varchar(100),
    environment varchar(100) NOT NULL DEFAULT '*',
    namespace varchar(253) NOT NULL DEFAULT '*',
    owner varchar(200),
    size_bytes integer NOT NULL DEFAULT 0,
    status varchar(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active','deleted','excluded')),
    excluded_reason text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    indexed_at timestamptz,
    UNIQUE (source_id, path)
);

CREATE TABLE chunks (
    id uuid PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ord integer NOT NULL,
    heading varchar(500) NOT NULL DEFAULT '',
    text text NOT NULL,
    start_line integer,
    end_line integer,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(heading, '') || ' ' || text)) STORED,
    UNIQUE (document_id, ord)
);
CREATE INDEX chunks_tsv ON chunks USING gin (tsv);

-- Versioned embedding indexes: model changes build a new version and switch after reindexing.
CREATE TABLE embedding_indexes (
    id uuid PRIMARY KEY,
    backend varchar(20) NOT NULL,
    llm_config_id uuid REFERENCES llm_configs(id) ON DELETE SET NULL,
    model_key varchar(300) NOT NULL,
    dimensions integer,
    index_name varchar(200) NOT NULL,
    status varchar(20) NOT NULL DEFAULT 'building' CHECK (status IN ('building','active','retired','failed')),
    chunk_count integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz
);
CREATE UNIQUE INDEX embedding_indexes_one_active ON embedding_indexes (backend) WHERE status = 'active';

CREATE TABLE chunk_embeddings (
    chunk_id uuid NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    index_id uuid NOT NULL REFERENCES embedding_indexes(id) ON DELETE CASCADE,
    PRIMARY KEY (chunk_id, index_id)
);

-- ---- Action plans and approvals (Phase 3) --------------------------------------------------
CREATE TABLE action_plans (
    id uuid PRIMARY KEY,
    incident_id uuid REFERENCES incidents(id) ON DELETE SET NULL,
    investigation_id uuid REFERENCES investigations(id) ON DELETE SET NULL,
    environment_id uuid NOT NULL REFERENCES environments(id),
    namespace varchar(253) NOT NULL,
    action varchar(40) NOT NULL,
    target_kind varchar(40) NOT NULL,
    target_name varchar(253) NOT NULL,
    params jsonb NOT NULL DEFAULT '{}',
    reason text NOT NULL,
    evidence jsonb NOT NULL DEFAULT '[]',
    expected_impact text NOT NULL DEFAULT '',
    blast_radius text NOT NULL DEFAULT '',
    preconditions jsonb NOT NULL DEFAULT '[]',
    validation jsonb NOT NULL DEFAULT '[]',
    rollback text NOT NULL DEFAULT '',
    auto_recover boolean NOT NULL DEFAULT true,
    pre_state jsonb,
    requires_approval boolean NOT NULL DEFAULT true,
    plan_hash varchar(64) NOT NULL,
    requested_by uuid NOT NULL REFERENCES users(id),
    status varchar(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','rejected','expired','queued','executing','succeeded','failed','recovered','cancelled')),
    expires_at timestamptz NOT NULL,
    executed_by uuid REFERENCES users(id),
    queued_at timestamptz,
    executed_at timestamptz,
    finished_at timestamptz,
    result jsonb,
    worker_id varchar(100),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX action_plans_queue ON action_plans (queued_at) WHERE status = 'queued';
CREATE INDEX action_plans_env ON action_plans (environment_id, created_at DESC);

CREATE TABLE approvals (
    id uuid PRIMARY KEY,
    plan_id uuid NOT NULL REFERENCES action_plans(id) ON DELETE CASCADE,
    approver_id uuid NOT NULL REFERENCES users(id),
    decision varchar(10) NOT NULL CHECK (decision IN ('approve','reject')),
    plan_hash varchar(64) NOT NULL,
    comment text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (plan_id, approver_id)
);

-- ---- Diagnostic sessions (Phase 3) ----------------------------------------------------------
CREATE TABLE diagnostic_sessions (
    id uuid PRIMARY KEY,
    environment_id uuid NOT NULL REFERENCES environments(id),
    plan_id uuid REFERENCES action_plans(id) ON DELETE SET NULL,
    image_key varchar(60) NOT NULL,
    image varchar(300) NOT NULL,
    packages jsonb NOT NULL DEFAULT '[]',
    elevated boolean NOT NULL DEFAULT false,
    ttl_minutes integer NOT NULL CHECK (ttl_minutes BETWEEN 5 AND 240),
    pod_name varchar(253),
    pod_namespace varchar(253),
    status varchar(20) NOT NULL DEFAULT 'requested'
        CHECK (status IN ('requested','starting','ready','expired','terminated','failed')),
    requested_by uuid NOT NULL REFERENCES users(id),
    reason text NOT NULL DEFAULT '',
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    expires_at timestamptz,
    ended_at timestamptz
);

CREATE TABLE diagnostic_commands (
    id bigserial PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES diagnostic_sessions(id) ON DELETE CASCADE,
    actor_id uuid NOT NULL REFERENCES users(id),
    tool varchar(40) NOT NULL,
    args jsonb NOT NULL DEFAULT '{}',
    argv jsonb NOT NULL DEFAULT '[]',
    exit_code integer,
    output text,
    status varchar(20) NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE INDEX diagnostic_commands_queue ON diagnostic_commands (created_at) WHERE status = 'queued';

-- ---- Alerts (Phase 4) -----------------------------------------------------------------------
CREATE TABLE alert_events (
    id bigserial PRIMARY KEY,
    environment_id uuid NOT NULL REFERENCES environments(id),
    source varchar(20) NOT NULL,
    fingerprint varchar(64) NOT NULL,
    alertname varchar(200) NOT NULL,
    status varchar(10) NOT NULL CHECK (status IN ('firing','resolved')),
    labels jsonb NOT NULL DEFAULT '{}',
    annotations jsonb NOT NULL DEFAULT '{}',
    starts_at timestamptz,
    received_at timestamptz NOT NULL DEFAULT now(),
    decision varchar(20) NOT NULL,
    incident_id uuid REFERENCES incidents(id) ON DELETE SET NULL,
    investigation_id uuid REFERENCES investigations(id) ON DELETE SET NULL
);
CREATE INDEX alert_events_fp ON alert_events (environment_id, fingerprint, received_at DESC);

-- ---- Evaluation (Phase 4) -------------------------------------------------------------------
CREATE TABLE eval_runs (
    id uuid PRIMARY KEY,
    dataset varchar(100) NOT NULL,
    mode varchar(20) NOT NULL,
    status varchar(20) NOT NULL DEFAULT 'running',
    score numeric(6,4),
    results jsonb NOT NULL DEFAULT '[]',
    created_by uuid REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
"""


def downgrade() -> None:
    raise RuntimeError("0002 is forward-only; restore from backup to go back.")
