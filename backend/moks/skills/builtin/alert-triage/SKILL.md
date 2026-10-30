---
name: alert-triage
description: "Explain a firing alert: its rule, current value, affected scope and supporting dashboards."
triggers:
  keywords: [alert, alerting, firing, alertname, pagerduty, page]
  findings: []
steps:
  - tool: prom_alerts
    args: {firing_only: false}
    purpose: rule expression, state and active instances
  - tool: grafana_alerts
    args: {firing_only: false}
    purpose: Grafana-managed rules, if any
  - tool: grafana_dashboards
    args: {query: "{namespace}"}
    purpose: dashboards to link as evidence
  - tool: kb_search
    args: {query: "{question} runbook"}
    purpose: the alert's runbook
title: Alert triage
owner: sre
category: support
version: '2'
---
# Alert triage

1. Quote the **rule expression** and the **current value** of each active instance; explain what the threshold means in plain words.
2. Identify the **scope** from labels (namespace, service, pod) and investigate that scope, not the whole cluster.
3. Distinguish **symptom alerts** (error ratio, latency) from **cause alerts** (target down, OOM); fix causes first.
4. If the alert has a `for:` clause, it has been true at least that long — use that to bound the onset.
5. Link the dashboard panel that shows the same series.

An alert that cannot be reproduced from its own query in the window is a monitoring problem — say so.
