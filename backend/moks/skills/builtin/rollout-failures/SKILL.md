---
name: rollout-failures
title: Deployment rollout failures
version: '1'
owner: sre
category: kubernetes
description: Explain a rollout that stalled or left replicas unavailable, and decide whether to wait, fix forward or roll
  back.
triggers:
  keywords:
  - rollout
  - deployment stuck
  - progress deadline
  - failed deploy
  - rollout stuck
  findings:
  - workload_unavailable
  - recent_rollout
steps:
- tool: k8s_rollout_history
  for_each: finding.workload_unavailable
  limit: 2
  args:
    namespace: '{namespace}'
    deployment: '{deployment}'
  purpose: recent rollouts
- tool: k8s_events
  args:
    namespace: '{namespace}'
  purpose: warning events
- tool: k8s_triage
  args:
    namespace: '{namespace}'
  purpose: pods, services and workloads
- tool: kb_search
  args:
    query: deployment rollout failure
  purpose: documented runbooks
resource_types:
- Deployment
hypotheses:
- statement: The new revision cannot start.
  supports: New pods crash or fail to pull.
  refutes: New pods are Ready and only capacity is short.
- statement: The cluster has no capacity for the surge.
  supports: New pods Pending with FailedScheduling.
  refutes: Pods scheduled.
evidence_required:
- rollout history
- state of pods in the new ReplicaSet
- warning events
verification:
- New ReplicaSet pods show the failing reason directly.
recovery_validation:
- updated = available = desired
- no pod of the new revision failing
evaluation:
- fixture: failed-rollout
  expect: workload_unavailable finding and a rollback hint
detection:
- id: rollout-unavailable
  kind: finding
  finding: workload_unavailable
  sustained_checks: 3
  severity: sev2
  auto_investigate: true
  auto_resolve: true
  title: Rollout not available
remediation:
- when: workload_unavailable
  action: rollback_deployment
  target: Deployment/{deployment}
  note: Only if the previous revision was healthy and the failure began with this rollout.
---
# Deployment rollout failures

Compare the new ReplicaSet with the old one. Pods of the old revision being healthy says nothing about the new one. A rollout past its progress deadline does not roll back on its own.
