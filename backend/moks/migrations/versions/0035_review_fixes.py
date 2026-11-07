"""Review fixes (docs/SECRETS_SYNC.md, docs/ML.md):

- secret_plan_items.connector_binding: each item is bound to its connectors as compared (plan hash version 2).
  Plans hashed as version 1 keep their rows and history and are refused at submit, approval and execution.
- secret_sync_schedules.status 'held': schedules held by the manual-only sync policy. Existing active, paused and
  pending schedules are held by the executor at its next tick, with the reason; approvals and runs stay.
- learning datasets freeze their contents: items keep the redacted exported record and its SHA-256, the checksum
  covers them (checksum_version 2). Existing datasets are checksum_version 1, reported as not reproducible. The
  foreign key from items to learning_records goes, so a purge no longer shrinks a dataset silently; instead a
  trigger invalidates every dataset holding a record that is deleted or whose approved label is withdrawn, and
  clears that dataset's snapshot. It runs as the migration owner (SECURITY DEFINER), so the worker's retention purge
  and the API's reviews fire it without write access to datasets.

Revision ID: 0035
Revises: 0034
"""
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("ALTER TABLE secret_plan_items ADD COLUMN IF NOT EXISTS connector_binding jsonb NOT NULL DEFAULT '{}'")

    bind.exec_driver_sql("ALTER TABLE secret_sync_schedules DROP CONSTRAINT IF EXISTS secret_sync_schedules_status_check")
    bind.exec_driver_sql("""ALTER TABLE secret_sync_schedules ADD CONSTRAINT secret_sync_schedules_status_check
                            CHECK (status IN ('pending', 'active', 'paused', 'held', 'rejected', 'retired'))""")

    bind.exec_driver_sql("ALTER TABLE learning_datasets ADD COLUMN IF NOT EXISTS checksum_version integer NOT NULL DEFAULT 1")
    bind.exec_driver_sql("ALTER TABLE learning_datasets ALTER COLUMN checksum_version SET DEFAULT 2")
    bind.exec_driver_sql("ALTER TABLE learning_datasets ADD COLUMN IF NOT EXISTS invalidated_at timestamptz")
    bind.exec_driver_sql("ALTER TABLE learning_datasets ADD COLUMN IF NOT EXISTS invalidated_reason text")
    bind.exec_driver_sql("ALTER TABLE learning_datasets DROP CONSTRAINT IF EXISTS learning_datasets_state_check")
    bind.exec_driver_sql("ALTER TABLE learning_datasets ADD CONSTRAINT learning_datasets_state_check CHECK (state IN ('built', 'retired', 'invalidated'))")
    bind.exec_driver_sql("ALTER TABLE learning_dataset_items ADD COLUMN IF NOT EXISTS payload jsonb")
    bind.exec_driver_sql("ALTER TABLE learning_dataset_items ADD COLUMN IF NOT EXISTS payload_sha256 varchar(64)")
    bind.exec_driver_sql("ALTER TABLE learning_dataset_items DROP CONSTRAINT IF EXISTS learning_dataset_items_record_id_fkey")
    bind.exec_driver_sql("CREATE INDEX IF NOT EXISTS learning_dataset_items_record ON learning_dataset_items (record_id)")
    bind.exec_driver_sql("""CREATE OR REPLACE FUNCTION moks_learning_record_withdrawn() RETURNS trigger AS $$
        DECLARE why text;
        BEGIN
          IF TG_OP = 'DELETE' THEN
            why := 'source record ' || OLD.id || ' was deleted (retention or its investigation was removed)';
          ELSIF OLD.label_state = 'approved' AND NEW.label_state <> 'approved' THEN
            why := 'the approved label of source record ' || OLD.id || ' was withdrawn';
          ELSE
            RETURN NEW;
          END IF;
          UPDATE learning_datasets SET state = 'invalidated', invalidated_at = now(), invalidated_reason = why
            WHERE state <> 'invalidated' AND id IN (SELECT dataset_id FROM learning_dataset_items WHERE record_id = OLD.id);
          UPDATE learning_dataset_items SET payload = NULL
            WHERE dataset_id IN (SELECT dataset_id FROM learning_dataset_items WHERE record_id = OLD.id) AND payload IS NOT NULL;
          RETURN COALESCE(NEW, OLD);
        END $$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public""")
    bind.exec_driver_sql("DROP TRIGGER IF EXISTS learning_records_withdrawn ON learning_records")
    bind.exec_driver_sql("""CREATE TRIGGER learning_records_withdrawn AFTER DELETE OR UPDATE OF label_state ON learning_records
                            FOR EACH ROW EXECUTE FUNCTION moks_learning_record_withdrawn()""")


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP TRIGGER IF EXISTS learning_records_withdrawn ON learning_records")
    bind.exec_driver_sql("DROP FUNCTION IF EXISTS moks_learning_record_withdrawn()")
    # Items whose record is gone cannot get their foreign key back; their datasets are invalidated already.
    bind.exec_driver_sql("DELETE FROM learning_dataset_items i WHERE NOT EXISTS (SELECT 1 FROM learning_records r WHERE r.id = i.record_id)")
    bind.exec_driver_sql("""ALTER TABLE learning_dataset_items ADD CONSTRAINT learning_dataset_items_record_id_fkey
                            FOREIGN KEY (record_id) REFERENCES learning_records(id) ON DELETE CASCADE""")
    bind.exec_driver_sql("DROP INDEX IF EXISTS learning_dataset_items_record")
    for c in ("payload", "payload_sha256"):
        bind.exec_driver_sql(f"ALTER TABLE learning_dataset_items DROP COLUMN IF EXISTS {c}")
    bind.exec_driver_sql("UPDATE learning_datasets SET state = 'retired' WHERE state = 'invalidated'")
    bind.exec_driver_sql("ALTER TABLE learning_datasets DROP CONSTRAINT IF EXISTS learning_datasets_state_check")
    bind.exec_driver_sql("ALTER TABLE learning_datasets ADD CONSTRAINT learning_datasets_state_check CHECK (state IN ('built', 'retired'))")
    for c in ("checksum_version", "invalidated_at", "invalidated_reason"):
        bind.exec_driver_sql(f"ALTER TABLE learning_datasets DROP COLUMN IF EXISTS {c}")

    bind.exec_driver_sql("UPDATE secret_sync_schedules SET status = 'paused' WHERE status = 'held'")
    bind.exec_driver_sql("ALTER TABLE secret_sync_schedules DROP CONSTRAINT IF EXISTS secret_sync_schedules_status_check")
    bind.exec_driver_sql("""ALTER TABLE secret_sync_schedules ADD CONSTRAINT secret_sync_schedules_status_check
                            CHECK (status IN ('pending', 'active', 'paused', 'rejected', 'retired'))""")
    bind.exec_driver_sql("ALTER TABLE secret_plan_items DROP COLUMN IF EXISTS connector_binding")
