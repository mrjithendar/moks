---
name: telemetry-health
title: Telemetry health
version: '1'
owner: sre
category: devops
description: Watch the telemetry itself. This is the only rule that treats missing data as a problem.
triggers:
  keywords:
  - telemetry
  - scrape
  - targets down
  - monitoring down
  - no metrics
  findings: []
steps:
- tool: prom_targets
  args: {}
  purpose: scrape targets down
resource_types:
- Job
- Instance
requires:
- prometheus
hypotheses:
- statement: The exporter is down.
  supports: One target is down.
  refutes: 'All targets are: look at Prometheus.'
evaluation:
- fixture: telemetry-outage
  expect:
    selected: true
    note: 'no false recovery'
detection:
- id: scrape-target-down
  kind: prometheus
  query: up
  operator: ==
  threshold: 0
  resource_label:
  - job
  - instance
  sustained_checks: 3
  interval_minutes: 5
  severity: sev3
  auto_resolve: true
  title: Scrape target down
---
# Telemetry health

While a target is down, rules using its metrics report unknown. They neither fire nor recover.
