---
name: release-regression
title: Release-correlated regression
version: '1'
owner: sre
category: application
description: Check whether a problem began with a release, and say plainly when it is only timing.
triggers:
  keywords:
  - after deploy
  - after release
  - regression
  - since the release
  - new version
  findings:
  - recent_rollout
steps:
- tool: k8s_rollout_history
  for_each: finding.recent_rollout
  limit: 2
  args:
    namespace: '{namespace}'
    deployment: '{deployment}'
  purpose: what changed and when
- tool: k8s_workloads
  args:
    namespace: '{namespace}'
  purpose: workload status
- tool: kb_search
  args:
    query: release notes known issues
  purpose: documented runbooks
resource_types:
- Deployment
hypotheses:
- statement: The release caused it.
  supports: Symptom starts at rollout; other services unaffected.
  refutes: Symptom predates the rollout.
verification:
- Symptom recovers after rollback.
remediation:
- when: recent_rollout
  action: rollback_deployment
  target: Deployment/{deployment}
  note: If symptoms start at the rollout and the previous revision was healthy.
---
# Release-correlated regression

Timing is correlation only. The strongest evidence is recovery after rollback, which is why it counts as verification.
