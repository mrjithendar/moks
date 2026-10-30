---
name: backup-health
title: Cluster backup health
version: '1'
owner: sre
category: kubernetes
description: Check that cluster backups are running, complete and restorable, and explain failures.
triggers:
  keywords:
  - backup
  - backups
  - restore
  - velero
  - disaster recovery
  findings: []
steps:
- tool: k8s_backup_status
  args: {}
  purpose: Velero location, backups, restores and schedules
resource_types:
- Namespace
telemetry: []
queries: {}
hypotheses:
- statement: The R2 location is unavailable.
  supports: Location phase is not Available; credential or encryption errors in its message.
  refutes: Location Available with a recent validation time.
- statement: Backups run but do not cover everything.
  supports: Partially failed backups; no backup with volume data; namespaces missing.
  refutes: Recent completed backups with volumes and no errors.
- statement: No schedule is running.
  supports: No schedule, a paused schedule, or an old last backup time.
  refutes: Schedule active with a recent last backup.
recovery_validation:
- a new backup completes with no errors
- a test restore into a spare namespace completes
---
# Cluster backup health

A backup only counts once a restore has been proven. Report the age of the last completed backup, whether it includes volume data, and any errors. Velero status holds names and counts only: never ask for Secret values.
