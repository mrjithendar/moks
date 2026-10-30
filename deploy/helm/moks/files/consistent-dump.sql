-- One snapshot for the dump and its manifest, so the row counts describe exactly what was dumped.
-- pg_dump joins this transaction's snapshot through --snapshot; a failure leaves a marker file.
\set ON_ERROR_STOP on
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT pg_export_snapshot() AS snapshot \gset
\setenv MOKS_SNAPSHOT :snapshot
\! pg_dump --snapshot="$MOKS_SNAPSHOT" --format=custom --compress=6 --file="$MOKS_DUMP_FILE" || touch "$MOKS_DUMP_FILE.failed"
\pset tuples_only on
\pset format unaligned
\o :manifest
SELECT json_build_object(
  'migration', (SELECT version_num FROM alembic_version),
  'snapshot_at', now(),
  'tables', (SELECT json_object_agg(tablename, (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM public.%I', tablename), false, true, '')))[1]::text::bigint)
             FROM pg_tables WHERE schemaname = 'public'));
\o
COMMIT;
