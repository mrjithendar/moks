"""Per-namespace detection identity, rotating namespace coverage, and renewable job leases.

Fingerprints used to hash only rule, environment and resource name, so identically named resources
in different namespaces shared state and incidents. New ones cannot be computed for existing rows
(the kind and owner were never stored), so:

- active detection incidents are marked legacy ("v1-" prefix) and noted on their timeline; the first
  matching detection adopts one, and anything else stays open for a person to review
- detection_states is transient and is cleared: counters rebuild within a few checks, and a resource
  already covered by an open incident keeps that incident through adoption

Revision ID: 0009
Revises: 0008
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for stmt in (
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz",
        "CREATE INDEX IF NOT EXISTS jobs_leases ON jobs (lease_expires_at) WHERE status = 'running'",
        "ALTER TABLE detection_runs ADD COLUMN IF NOT EXISTS cursor integer NOT NULL DEFAULT 0",
        """INSERT INTO incident_events (incident_id, kind, payload)
           SELECT id, 'note', '{"text": "Detection identity changed: this incident predates per-namespace fingerprints. A matching detection adopts it; otherwise review and close it."}'::jsonb
           FROM incidents WHERE source = 'detection' AND status IN ('open','mitigated')""",
        "UPDATE incidents SET fingerprint = 'v1-' || left(fingerprint, 40) WHERE source = 'detection' AND fingerprint IS NOT NULL AND fingerprint NOT LIKE 'v1-%'",
        "DELETE FROM detection_states",
    ):
        bind.exec_driver_sql(stmt)


def downgrade() -> None:
    raise RuntimeError("forward-only")
