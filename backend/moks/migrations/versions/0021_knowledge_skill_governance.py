"""Governed knowledge sources, resumable ingestion, per-index state, and a versioned skill registry. Additive.

Existing sources scoped to every environment and namespace are recorded as globally shared, because
that is what they already were. Existing documents get pipeline version 0, so the next sync re-chunks
them without losing what is indexed today.

Revision ID: 0021
Revises: 0020
"""
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run = op.get_bind().exec_driver_sql
    run("""
        ALTER TABLE knowledge_sources
            ADD COLUMN IF NOT EXISTS lifecycle varchar(20) NOT NULL DEFAULT 'active',
            ADD COLUMN IF NOT EXISTS team varchar(120),
            ADD COLUMN IF NOT EXISTS classification varchar(20) NOT NULL DEFAULT 'internal',
            ADD COLUMN IF NOT EXISTS shared_globally boolean NOT NULL DEFAULT false,
            ADD COLUMN IF NOT EXISTS applies_to jsonb NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS authority integer NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS review_interval_days integer,
            ADD COLUMN IF NOT EXISTS last_reviewed_at timestamptz,
            ADD COLUMN IF NOT EXISTS stale_policy varchar(20) NOT NULL DEFAULT 'flag',
            ADD COLUMN IF NOT EXISTS stale_after_days integer,
            ADD COLUMN IF NOT EXISTS sync_cursor jsonb NOT NULL DEFAULT '{}'::jsonb""")
    run("UPDATE knowledge_sources SET shared_globally = true WHERE scope_environment = '*' AND scope_namespace = '*'")
    run("""
        ALTER TABLE documents
            ADD COLUMN IF NOT EXISTS pipeline_version integer NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS changed_revision varchar(100),
            ADD COLUMN IF NOT EXISTS checked_at timestamptz,
            ADD COLUMN IF NOT EXISTS superseded_by varchar(1000),
            ADD COLUMN IF NOT EXISTS doc_state varchar(20) NOT NULL DEFAULT 'current',
            ADD COLUMN IF NOT EXISTS vector_state varchar(20) NOT NULL DEFAULT 'unknown',
            ADD COLUMN IF NOT EXISTS vector_error text""")
    run("UPDATE documents SET changed_revision = revision WHERE changed_revision IS NULL")
    run("""
        ALTER TABLE chunks
            ADD COLUMN IF NOT EXISTS start_offset integer,
            ADD COLUMN IF NOT EXISTS end_offset integer,
            ADD COLUMN IF NOT EXISTS token_estimate integer,
            ADD COLUMN IF NOT EXISTS text_hash varchar(16)""")
    run("""
        CREATE TABLE IF NOT EXISTS knowledge_sync_items (
            source_id uuid NOT NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE,
            path varchar(1000) NOT NULL,
            blob_sha varchar(64) NOT NULL,
            size_bytes bigint NOT NULL DEFAULT 0,
            revision varchar(100) NOT NULL,
            state varchar(20) NOT NULL DEFAULT 'pending',
            attempts integer NOT NULL DEFAULT 0,
            last_error text,
            last_tried_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (source_id, path))""")
    run("CREATE INDEX IF NOT EXISTS ix_sync_items_work ON knowledge_sync_items (source_id, state, last_tried_at)")
    run("""
        CREATE TABLE IF NOT EXISTS document_index_state (
            document_id uuid NOT NULL,
            index_id uuid NOT NULL REFERENCES embedding_indexes(id) ON DELETE CASCADE,
            revision bigint NOT NULL DEFAULT 0,
            chunks integer NOT NULL DEFAULT 0,
            vectors integer NOT NULL DEFAULT 0,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (document_id, index_id))""")
    run("""
        CREATE TABLE IF NOT EXISTS skill_versions (
            digest varchar(64) PRIMARY KEY,
            skill_id varchar(120) NOT NULL,
            version varchar(40) NOT NULL,
            owner varchar(120) NOT NULL DEFAULT 'sre',
            source varchar(20) NOT NULL DEFAULT 'builtin',
            content text NOT NULL,
            meta jsonb NOT NULL DEFAULT '{}'::jsonb,
            state varchar(20) NOT NULL DEFAULT 'draft',
            validation jsonb NOT NULL DEFAULT '[]'::jsonb,
            reviewed_by uuid REFERENCES users(id),
            reviewed_at timestamptz,
            published_by uuid REFERENCES users(id),
            published_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (skill_id, version))""")
    run("""
        CREATE TABLE IF NOT EXISTS skill_generations (
            generation bigserial PRIMARY KEY,
            digest varchar(64) NOT NULL,
            manifest jsonb NOT NULL,
            status varchar(20) NOT NULL DEFAULT 'active',
            reason text NOT NULL DEFAULT '',
            created_by uuid REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now())""")
    run("""
        ALTER TABLE component_versions
            ADD COLUMN IF NOT EXISTS skill_generation bigint,
            ADD COLUMN IF NOT EXISTS skill_digest varchar(64),
            ADD COLUMN IF NOT EXISTS skill_error text""")
    run("""
        ALTER TABLE skill_policies
            ADD COLUMN IF NOT EXISTS environments jsonb NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN IF NOT EXISTS allow_override boolean NOT NULL DEFAULT false""")
    run("""
        ALTER TABLE investigations
            ADD COLUMN IF NOT EXISTS requested_skill varchar(120),
            ADD COLUMN IF NOT EXISTS skill_generation bigint,
            ADD COLUMN IF NOT EXISTS skill_pins jsonb NOT NULL DEFAULT '[]'::jsonb""")
    run("""
        ALTER TABLE detection_runs
            ADD COLUMN IF NOT EXISTS skill varchar(120),
            ADD COLUMN IF NOT EXISTS skill_version varchar(40),
            ADD COLUMN IF NOT EXISTS skill_digest varchar(64),
            ADD COLUMN IF NOT EXISTS skill_generation bigint""")
    run("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS skill_digest varchar(64)")
    run("""
        ALTER TABLE eval_runs
            ADD COLUMN IF NOT EXISTS kind varchar(20) NOT NULL DEFAULT 'investigation',
            ADD COLUMN IF NOT EXISTS versions jsonb NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS gates jsonb NOT NULL DEFAULT '[]'::jsonb""")


def downgrade() -> None:
    raise RuntimeError("forward-only")
