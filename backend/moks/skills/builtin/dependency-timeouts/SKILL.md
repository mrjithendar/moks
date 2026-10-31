---
name: dependency-timeouts
title: Dependency timeouts
version: '1'
owner: sre
category: application
description: Trace timeouts to the dependency that is slow, and separate cause from cascade.
triggers:
  keywords:
  - timeout
  - timed out
  - dependency
  - upstream
  - deadline exceeded
  findings: []
steps:
- tool: loki_logs
  args:
    contains: timeout
    limit: 100
  purpose: log lines with timeout
- tool: loki_logs
  args:
    contains: deadline exceeded
    limit: 100
  purpose: log lines with deadline exceeded
- tool: k8s_triage
  args:
    namespace: '{namespace}'
  purpose: pods, services and workloads
- tool: k8s_dns_health
  args: {}
  purpose: CoreDNS health
resource_types:
- Application service
- Service
hypotheses:
- statement: The dependency is slow or down.
  supports: All callers time out on one upstream.
  refutes: Only one caller is affected.
- statement: The caller is exhausted.
  supports: Local CPU or pool saturation.
  refutes: Caller has headroom.
evaluation:
- fixture: dependency-latency
  expect:
    selected: true
    note: 'latency tied to the inventory rollout, marked as timing correlation'
detection:
- id: dependency-timeouts
  kind: loki
  query: timeout
  operator: '>='
  threshold: 20
  lookback_minutes: 5
  sustained_checks: 2
  severity: sev3
  title: Timeouts in logs
---
# Dependency timeouts

Find the deepest failing call. Services above it fail because of it, so their errors are symptoms.
