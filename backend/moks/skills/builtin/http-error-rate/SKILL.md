---
name: http-error-rate
title: Elevated HTTP error rate
version: '1'
owner: sre
category: application
description: Find which service and route are returning errors, since when, and what changed.
triggers:
  keywords:
  - error rate
  - 5xx
  - 500 errors
  - errors increased
  - http errors
  findings:
  - recent_rollout
steps:
- tool: prom_query
  args:
    promql: topk(10, sum by (job) (rate(http_requests_total{status=~"5.."}[5m])))
    mode: instant
  purpose: services returning 5xx
- tool: loki_logs
  args:
    contains: error
    limit: 100
  purpose: log lines with error
- tool: k8s_triage
  args:
    namespace: '{namespace}'
  purpose: pods, services and workloads
resource_types:
- Application service
telemetry:
- http_requests_total
queries:
  error_ratio:
    language: promql
    query: sum by (job) (rate(http_requests_total{status=~"5.."}[5m])) / sum by (job) (rate(http_requests_total[5m]))
    purpose: share of failing requests
hypotheses:
- statement: A release introduced the errors.
  supports: Errors start at the rollout.
  refutes: Errors began earlier.
- statement: A dependency is failing.
  supports: Timeouts to one upstream.
  refutes: All upstreams healthy.
recovery_validation:
- error ratio below 1% for 15 minutes
detection:
- id: http-error-ratio
  kind: prometheus
  query: sum by (job) (rate(http_requests_total{status=~"5.."}[5m])) / sum by (job) (rate(http_requests_total[5m]))
  operator: '>'
  threshold: 0.05
  resource_label:
  - job
  sustained_checks: 3
  severity: sev2
  auto_investigate: true
  auto_resolve: true
  title: HTTP error ratio above 5%
---
# Elevated HTTP error rate

Use a ratio, not a count: traffic growth raises the count without anything being wrong.
