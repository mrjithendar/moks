---
name: rds-pressure
title: RDS pressure
version: '1'
owner: sre
category: aws
description: Investigate RDS connection, storage and latency pressure.
triggers:
  keywords:
  - rds
  - database
  - postgres
  - mysql
  - aurora
  - db connections
  findings: []
steps:
- tool: cw_metric
  args:
    namespace: AWS/RDS
    metric: DatabaseConnections
    stat: Maximum
  purpose: DatabaseConnections from CloudWatch
- tool: cw_metric
  args:
    namespace: AWS/RDS
    metric: FreeStorageSpace
    stat: Minimum
  purpose: FreeStorageSpace from CloudWatch
- tool: cw_metric
  args:
    namespace: AWS/RDS
    metric: ReadLatency
    stat: Average
  purpose: ReadLatency from CloudWatch
- tool: cw_alarms
  args:
    state: ALARM
  purpose: CloudWatch alarms in ALARM
resource_types:
- RDS
hypotheses:
- statement: Connections are exhausted.
  supports: Connections near max_connections.
  refutes: Plenty free.
detection:
- id: rds-alarm
  kind: cloudwatch_alarm
  query: (?i)(rds|aurora|database)
  sustained_checks: 1
  severity: sev2
  title: Database alarm
---
# RDS pressure

Falling FreeStorageSpace with stable data often means transaction logs or a stuck replication slot.
