"""What each index generation really holds per document, and a storage boundary around evaluation content.

Every existing source is operational. Existing index rows have no expected counts, so they read as
unverified until the next write or reindex, rather than as complete.

Revision ID: 0027
Revises: 0026
"""
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("""ALTER TABLE document_index_state
        ADD COLUMN expected_chunks integer, ADD COLUMN expected_vectors integer, ADD COLUMN model_key varchar(200), ADD COLUMN dimensions integer,
        ADD COLUMN error text, ADD COLUMN retries integer NOT NULL DEFAULT 0""")
    bind.exec_driver_sql("ALTER TABLE knowledge_sources ADD COLUMN realm varchar(20) NOT NULL DEFAULT 'operational'")
    bind.exec_driver_sql("ALTER TABLE documents ADD COLUMN realm varchar(20) NOT NULL DEFAULT 'operational'")
    bind.exec_driver_sql("ALTER TABLE knowledge_sources ADD CONSTRAINT knowledge_sources_realm_check CHECK (realm IN ('operational', 'evaluation'))")
    bind.exec_driver_sql("ALTER TABLE documents ADD CONSTRAINT documents_realm_check CHECK (realm IN ('operational', 'evaluation'))")
    # A document can never sit in a different realm from its source, whatever code writes it.
    bind.exec_driver_sql("ALTER TABLE knowledge_sources ADD CONSTRAINT knowledge_sources_id_realm UNIQUE (id, realm)")
    bind.exec_driver_sql("ALTER TABLE documents ADD CONSTRAINT documents_source_realm FOREIGN KEY (source_id, realm) REFERENCES knowledge_sources (id, realm)")
    bind.exec_driver_sql("ALTER TABLE embedding_indexes ADD COLUMN activation varchar(20)")
    bind.exec_driver_sql("CREATE INDEX ix_documents_realm ON documents (realm) WHERE realm <> 'operational'")


def downgrade() -> None:
    raise RuntimeError("forward-only")
