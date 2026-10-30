---
name: connection-pool-exhaustion
title: Connection pool exhaustion
version: '1'
owner: sre
category: application
description: Recognise exhausted connection pools and find what is holding the connections.
triggers:
  keywords:
  - connection pool
  - too many connections
  - pool exhausted
  - max connections
  findings: []
steps:
- tool: loki_logs
  args:
    contains: too many connections
    limit: 100
  purpose: log lines with too many connections
- tool: loki_logs
  args:
    contains: pool
    limit: 100
  purpose: log lines with pool
- tool: k8s_triage
  args:
    namespace: '{namespace}'
  purpose: pods, services and workloads
resource_types:
- Application service
- RDS
hypotheses:
- statement: Slow queries hold connections.
  supports: Latency rose first.
  refutes: Query time normal.
- statement: A leak holds connections.
  supports: In-use count only grows.
  refutes: Count falls with load.
---
# Connection pool exhaustion

Raising the pool size usually moves the failure to the database. Check what holds connections first.
