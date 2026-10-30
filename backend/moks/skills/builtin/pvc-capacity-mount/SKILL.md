---
name: pvc-capacity-mount
title: PVC capacity and mount failures
version: '1'
owner: sre
category: kubernetes
description: Diagnose volumes that are full, failing to mount or stuck attaching.
triggers:
  keywords:
  - pvc
  - volume
  - mount
  - disk full
  - no space left
  - persistent volume
  - attach
  findings: []
steps:
- tool: k8s_events
  args:
    namespace: '{namespace}'
  purpose: warning events
- tool: prom_query
  args:
    promql: topk(10, kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes)
    mode: instant
  purpose: fullest volumes
- tool: loki_logs
  args:
    contains: no space left
    limit: 100
  purpose: log lines with no space left
resource_types:
- PersistentVolumeClaim
- Pod
telemetry:
- kubelet_volume_stats_used_bytes
hypotheses:
- statement: The volume is full.
  supports: Usage near capacity and 'no space left' in logs.
  refutes: Usage well below capacity.
recovery_validation:
- usage below 80%
- pods mount the volume
detection:
- id: pvc-nearly-full
  kind: prometheus
  query: kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes
  operator: '>'
  threshold: 0.9
  resource_label:
  - namespace
  - persistentvolumeclaim
  sustained_checks: 2
  interval_minutes: 15
  severity: sev3
  auto_resolve: true
  title: Volume over 90% full
---
# PVC capacity and mount failures

FailedMount and FailedAttachVolume events name the failing step. Do not delete data to free space during an incident without the owner.
