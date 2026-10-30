"""Phase 1 schema: users, RBAC, sessions, environments, connectors, LLM configs,
investigations, evidence and the append-only audit log.

Revision ID: 0001
Revises:
"""
from alembic import op

from moks.migrations import split_sql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in split_sql(SCHEMA):
        op.get_bind().exec_driver_sql(statement)  # bypass text() so ":MI" etc. are not bind params


SCHEMA = (
        """
        CREATE TABLE users (
            id uuid PRIMARY KEY,
            email varchar(320) NOT NULL UNIQUE,
            display_name varchar(200) NOT NULL DEFAULT '',
            auth_provider varchar(20) NOT NULL CHECK (auth_provider IN ('oidc','local','dev')),
            subject varchar(255),
            password_hash text,
            is_active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            last_login_at timestamptz
        );
        CREATE UNIQUE INDEX users_oidc_subject ON users (subject) WHERE auth_provider = 'oidc';

        CREATE TABLE role_bindings (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role varchar(40) NOT NULL
                CHECK (role IN ('viewer','investigator','operator','approver','admin')),
            environment varchar(100) NOT NULL DEFAULT '*',
            namespace varchar(253) NOT NULL DEFAULT '*',
            source varchar(20) NOT NULL DEFAULT 'manual',
            created_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (user_id, role, environment, namespace)
        );

        CREATE TABLE sessions (
            id varchar(64) PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            csrf_token varchar(64) NOT NULL,
            id_token_hint text,
            created_at timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            revoked_at timestamptz,
            ip varchar(64),
            user_agent varchar(300)
        );
        CREATE INDEX sessions_user ON sessions (user_id);

        CREATE TABLE environments (
            id uuid PRIMARY KEY,
            name varchar(100) NOT NULL UNIQUE CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,99}$'),
            display_name varchar(200) NOT NULL DEFAULT '',
            kind varchar(20) NOT NULL CHECK (kind IN ('minikube','eks')),
            is_production boolean NOT NULL DEFAULT false,
            allowed_namespaces varchar[] NOT NULL DEFAULT '{}',
            denied_namespaces varchar[] NOT NULL DEFAULT '{}',
            max_query_window_minutes integer NOT NULL DEFAULT 360 CHECK (max_query_window_minutes BETWEEN 5 AND 10080),
            grafana_url text,
            created_at timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE connectors (
            id uuid PRIMARY KEY,
            environment_id uuid NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
            type varchar(30) NOT NULL,
            name varchar(100) NOT NULL,
            enabled boolean NOT NULL DEFAULT true,
            config jsonb NOT NULL DEFAULT '{}',
            credential_ref jsonb,
            last_test_at timestamptz,
            last_test_ok boolean,
            last_test_message text,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (environment_id, name)
        );

        CREATE TABLE llm_configs (
            id uuid PRIMARY KEY,
            name varchar(100) NOT NULL UNIQUE,
            purpose varchar(20) NOT NULL CHECK (purpose IN ('chat','embedding')),
            provider varchar(20) NOT NULL CHECK (provider IN ('openai','gemini','bedrock','ollama')),
            model varchar(200) NOT NULL,
            endpoint text,
            region varchar(40),
            credential_ref jsonb,
            timeout_seconds integer NOT NULL DEFAULT 60 CHECK (timeout_seconds BETWEEN 1 AND 600),
            max_output_tokens integer NOT NULL DEFAULT 2000,
            daily_budget_usd numeric(12,4) NOT NULL DEFAULT 5,
            input_price_per_mtok numeric(12,4) NOT NULL DEFAULT 0,
            output_price_per_mtok numeric(12,4) NOT NULL DEFAULT 0,
            embedding_dimensions integer,
            is_active boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        -- At most one active configuration per purpose.
        CREATE UNIQUE INDEX llm_configs_one_active ON llm_configs (purpose) WHERE is_active;

        CREATE TABLE llm_usage (
            id bigserial PRIMARY KEY,
            llm_config_id uuid REFERENCES llm_configs(id) ON DELETE SET NULL,
            investigation_id uuid,
            provider varchar(20) NOT NULL,
            model varchar(200) NOT NULL,
            tokens_in integer NOT NULL DEFAULT 0,
            tokens_out integer NOT NULL DEFAULT 0,
            cost_usd numeric(12,6) NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX llm_usage_day ON llm_usage (llm_config_id, created_at);

        CREATE TABLE investigations (
            id uuid PRIMARY KEY,
            created_by uuid NOT NULL REFERENCES users(id),
            environment_id uuid NOT NULL REFERENCES environments(id),
            namespace varchar(253) NOT NULL,
            question text NOT NULL,
            status varchar(20) NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','running','completed','failed','cancelled')),
            cancel_requested boolean NOT NULL DEFAULT false,
            time_from timestamptz NOT NULL,
            time_to timestamptz NOT NULL,
            report jsonb,
            error text,
            mode varchar(20) NOT NULL DEFAULT 'llm',
            tool_calls integer NOT NULL DEFAULT 0,
            tokens_in integer NOT NULL DEFAULT 0,
            tokens_out integer NOT NULL DEFAULT 0,
            cost_usd numeric(12,6) NOT NULL DEFAULT 0,
            worker_id varchar(100),
            heartbeat_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz,
            finished_at timestamptz,
            CHECK (time_from < time_to)
        );
        CREATE INDEX investigations_queue ON investigations (created_at) WHERE status = 'queued';
        CREATE INDEX investigations_env ON investigations (environment_id, created_at DESC);

        CREATE TABLE investigation_events (
            id bigserial PRIMARY KEY,
            investigation_id uuid NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
            seq integer NOT NULL,
            type varchar(30) NOT NULL,
            payload jsonb NOT NULL DEFAULT '{}',
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (investigation_id, seq)
        );

        CREATE TABLE evidence (
            id uuid PRIMARY KEY,
            investigation_id uuid NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
            ref varchar(10) NOT NULL,
            tool varchar(60) NOT NULL,
            connector_id uuid,
            connector_type varchar(30) NOT NULL,
            args jsonb NOT NULL DEFAULT '{}',
            scope jsonb NOT NULL DEFAULT '{}',
            ok boolean NOT NULL DEFAULT true,
            summary text NOT NULL DEFAULT '',
            data jsonb,
            source jsonb NOT NULL DEFAULT '{}',
            truncated boolean NOT NULL DEFAULT false,
            error text,
            duration_ms integer NOT NULL DEFAULT 0,
            collected_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (investigation_id, ref)
        );

        CREATE TABLE audit_events (
            id bigserial PRIMARY KEY,
            ts timestamptz NOT NULL DEFAULT now(),
            actor_id uuid,
            actor_email varchar(320),
            action varchar(80) NOT NULL,
            resource_type varchar(60),
            resource_id varchar(200),
            environment varchar(100),
            namespace varchar(253),
            decision varchar(10) NOT NULL DEFAULT 'n/a' CHECK (decision IN ('allow','deny','n/a')),
            outcome varchar(20) NOT NULL DEFAULT 'success',
            details jsonb NOT NULL DEFAULT '{}',
            ip varchar(64),
            request_id varchar(64),
            chain_seq bigint NOT NULL DEFAULT 0,
            prev_hash varchar(64) NOT NULL DEFAULT '',
            hash varchar(64) NOT NULL DEFAULT ''
        );
        CREATE INDEX audit_ts ON audit_events (ts DESC);
        CREATE INDEX audit_actor ON audit_events (actor_id, ts DESC);
        CREATE UNIQUE INDEX audit_chain ON audit_events (chain_seq);

        -- Hash of one audit row's content, chained to its predecessor. Shared by the insert
        -- trigger and by verification (moks verify-audit).
        CREATE FUNCTION moks_audit_row_hash(prev text, seq bigint, r audit_events) RETURNS text
        LANGUAGE sql STABLE AS $$
          SELECT encode(sha256(convert_to(concat_ws('|',
            prev, seq::text,
            to_char(r.ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
            coalesce(r.actor_id::text, ''), coalesce(r.actor_email, ''), r.action,
            coalesce(r.resource_type, ''), coalesce(r.resource_id, ''),
            coalesce(r.environment, ''), coalesce(r.namespace, ''),
            r.decision, r.outcome, r.details::text, coalesce(r.ip, ''), coalesce(r.request_id, '')
          ), 'UTF8')), 'hex')
        $$;

        CREATE FUNCTION moks_audit_chain() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          last_hash text;
          last_seq bigint;
        BEGIN
          -- Serialise appends so every row links to the row committed before it.
          PERFORM pg_advisory_xact_lock(7273110);
          SELECT hash, chain_seq INTO last_hash, last_seq FROM audit_events ORDER BY chain_seq DESC LIMIT 1;
          NEW.chain_seq := coalesce(last_seq, 0) + 1;
          NEW.prev_hash := coalesce(last_hash, repeat('0', 64));
          NEW.hash := moks_audit_row_hash(NEW.prev_hash, NEW.chain_seq, NEW);
          RETURN NEW;
        END $$;

        CREATE FUNCTION moks_audit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'audit_events is append-only';
        END $$;

        CREATE TRIGGER audit_events_chain BEFORE INSERT ON audit_events
          FOR EACH ROW EXECUTE FUNCTION moks_audit_chain();
        CREATE TRIGGER audit_events_no_update BEFORE UPDATE OR DELETE ON audit_events
          FOR EACH ROW EXECUTE FUNCTION moks_audit_immutable();
        CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON audit_events
          FOR EACH STATEMENT EXECUTE FUNCTION moks_audit_immutable();
        """
    )


def downgrade() -> None:
    raise RuntimeError("0001 cannot be downgraded: it would destroy the audit log. Restore from backup instead.")
