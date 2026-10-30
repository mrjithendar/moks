---
name: cloudwatch-ingestion-gaps
title: CloudWatch ingestion gaps
version: '1'
owner: sre
category: aws
description: Find logs or metrics that stopped arriving, so silence is not read as health.
triggers:
  keywords:
  - cloudwatch
  - missing logs
  - no logs
  - ingestion
  - metrics missing
  findings: []
steps:
- tool: cw_log_groups
  args: {}
  purpose: log groups and recency
- tool: cw_alarms
  args:
    state: ALARM
  purpose: CloudWatch alarms in ALARM
resource_types:
- CloudWatch namespace
hypotheses:
- statement: The agent stopped shipping.
  supports: All groups from one host stopped.
  refutes: Others still arrive.
---
# CloudWatch ingestion gaps

INSUFFICIENT_DATA is not OK. An alarm that loses its data stops protecting you.
