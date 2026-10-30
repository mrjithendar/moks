---
name: cpu-throttling
title: CPU throttling
version: '1'
owner: sre
category: kubernetes
description: Find containers held back by CPU limits, which shows up as latency without high average CPU.
triggers:
  keywords:
  - throttling
  - throttled
  - cpu limit
  - cfs
  findings: []
steps:
- tool: prom_query
  args:
    promql: topk(10, sum by (namespace, pod) (rate(container_cpu_cfs_throttled_periods_total[5m])) / sum by (namespace, pod)
      (rate(container_cpu_cfs_periods_total[5m])))
    mode: instant
  purpose: most throttled pods
- tool: kb_search
  args:
    query: cpu limits throttling
  purpose: documented runbooks
resource_types:
- Pod
- Container
telemetry:
- container_cpu_cfs_throttled_periods_total
queries:
  throttle_ratio:
    language: promql
    query: sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{namespace="$ns"}[5m])) / sum by (pod) (rate(container_cpu_cfs_periods_total{namespace="$ns"}[5m]))
    purpose: share of CFS periods throttled
hypotheses:
- statement: The CPU limit is too low for the load.
  supports: Throttling rises with traffic.
  refutes: Throttling is flat while latency moves.
evidence_required:
- throttled ratio over the window
recovery_validation:
- throttle ratio below 25% under normal traffic
detection:
- id: cpu-throttling-high
  kind: prometheus
  query: sum by (namespace, pod) (rate(container_cpu_cfs_throttled_periods_total[5m])) / sum by (namespace, pod) (rate(container_cpu_cfs_periods_total[5m]))
  operator: '>'
  threshold: 0.5
  resource_label:
  - namespace
  - pod
  sustained_checks: 3
  interval_minutes: 10
  severity: sev4
  empty_means_ok: false
  auto_resolve: true
  title: CPU throttling above 50%
remediation:
- when: under_provisioned
  action: patch_resources
  target: Deployment/{deployment}
  note: Raise the limit, or remove it if your policy allows.
---
# CPU throttling

Average CPU can look fine while a container is throttled in every period. Compare the ratio with latency over the same window before changing limits.
