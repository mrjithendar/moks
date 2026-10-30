---
name: queue-backlog
title: Queue backlog
version: '1'
owner: sre
category: application
description: Find whether a backlog comes from slower consumers or more producers.
triggers:
  keywords:
  - queue
  - backlog
  - lag
  - consumer lag
  - kafka
  - rabbitmq
  findings: []
steps:
- tool: loki_logs
  args:
    contains: backlog
    limit: 100
  purpose: log lines with backlog
- tool: k8s_workloads
  args:
    namespace: '{namespace}'
  purpose: workload status
resource_types:
- Application service
hypotheses:
- statement: Consumers slowed down.
  supports: Processing rate fell.
  refutes: Processing steady, intake rose.
---
# Queue backlog

Compare arrival rate with processing rate. Metric names vary by broker, so check what exists under Metrics first.
