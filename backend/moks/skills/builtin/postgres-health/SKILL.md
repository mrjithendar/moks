---
name: postgres-health
title: PostgreSQL health
version: '1'
owner: sre
category: application
description: Find why a PostgreSQL cluster is slow, refusing connections, locking up or falling behind, from the database's own statistics.
triggers:
  keywords:
  - postgres
  - postgresql
  - database
  - deadlock
  - lock wait
  - too many connections
  - idle in transaction
  - replication lag
  - vacuum
  - wraparound
  - slow query
  findings: []
steps:
- id: clusters
  tool: datastore_clusters
  args:
    kind: postgres
  purpose: PostgreSQL clusters in scope with their findings
  required: true
- tool: pg_locks
  args:
    cluster: '{cluster}'
  for_each: finding.pg_blocked
  limit: 2
  purpose: blocking chains and the root blocker
- tool: pg_sessions
  args:
    cluster: '{cluster}'
    min_age_seconds: 300
  for_each: finding.pg_long_xact
  limit: 2
  purpose: the long and idle-in-transaction sessions
- tool: pg_overview
  args:
    cluster: '{cluster}'
  for_each: finding.pg_connections
  limit: 2
  purpose: who holds the connections, by client and state
- tool: pg_replication
  args:
    cluster: '{cluster}'
  for_each: finding.pg_replica_lag
  limit: 2
  purpose: replica lag and slots
- tool: pg_replication
  args:
    cluster: '{cluster}'
  for_each: finding.pg_slot_retained
  limit: 2
  purpose: slots holding WAL back
- tool: pg_maintenance
  args:
    cluster: '{cluster}'
  for_each: finding.pg_xid_age
  limit: 2
  purpose: transaction-ID age and vacuum progress
- tool: pg_top_queries
  args:
    cluster: '{cluster}'
    order: total_time
  for_each: finding.pg_connections
  limit: 1
  purpose: statements holding the most time
resource_types:
- Postgres
hypotheses:
- statement: A session holds a lock and blocks the others.
  supports: Root blocker idle in transaction or long-running; many sessions waiting on it.
  refutes: No lock waits.
- statement: The application leaks or over-allocates connections.
  supports: Connections near max_connections, dominated by one client, many idle or idle in transaction.
  refutes: Connection use well below the limit.
- statement: A replica or replication slot falls behind.
  supports: Replay lag growing; an inactive slot retaining WAL.
  refutes: Replicas streaming with lag in seconds and no inactive slot.
- statement: Vacuum cannot keep up.
  supports: High dead-row share, old last_autovacuum, transaction-ID age rising, a long transaction holding xmin.
  refutes: Recent autovacuum on the busy tables and low xid age.
recovery_validation:
- datastore_clusters reports no blocked sessions and connection use below the warning threshold for the cluster
detection:
- id: pg-connections-saturated
  kind: datastore
  finding: pg_connections
  sustained_checks: 2
  severity: sev2
  title: PostgreSQL connections near the limit
- id: pg-lock-contention
  kind: datastore
  finding: pg_blocked
  sustained_checks: 2
  severity: sev2
  title: PostgreSQL sessions blocked on locks
- id: pg-long-transaction
  kind: datastore
  finding: pg_long_xact
  sustained_checks: 2
  severity: sev3
  title: Long-running PostgreSQL transaction
- id: pg-replication-lag
  kind: datastore
  finding: pg_replica_lag
  sustained_checks: 3
  severity: sev2
  title: PostgreSQL replica lagging
- id: pg-slot-retaining-wal
  kind: datastore
  finding: pg_slot_retained
  sustained_checks: 2
  severity: sev2
  title: Replication slot retaining WAL
- id: pg-xid-wraparound
  kind: datastore
  finding: pg_xid_age
  sustained_checks: 1
  severity: sev2
  title: PostgreSQL transaction-ID age high
---
# PostgreSQL health

Start from the database's own view, then go back to the application that holds the sessions.

1. **Lock waits.** Name the root blocker: its pid, application, state and how long its transaction has been open. A root blocker that is *idle in transaction* is an application bug (a missing commit or rollback), not a database problem.
2. **Connections.** Compare the total with `max_connections` and say which client holds most of them. Raising `max_connections` moves the failure; a pooler (PgBouncer) or fixing the leak removes it.
3. **Replication.** Lag in seconds on a primary that is idle is not a problem. An inactive slot that retains WAL fills the disk, so name the slot and its consumer.
4. **Transaction-ID age.** Above 1 billion is urgent: find the transaction or slot holding `xmin` back before tuning autovacuum.

Query text is normalised; never ask for raw values. Remedies that end sessions need an approved action plan.
