---
name: gitops-drift-argocd
title: GitOps drift
version: '1'
owner: sre
category: devops
description: Compare the running workload with what Git declares, and stay honest when revision metadata is missing.
triggers:
  keywords:
  - drift
  - argocd
  - argo cd
  - out of sync
  - gitops
  - sync failed
  findings:
  - recent_rollout
steps:
- tool: k8s_workloads
  args:
    namespace: '{namespace}'
  purpose: workload status
- tool: git_compare_deployment
  for_each: finding.recent_rollout
  limit: 2
  args:
    namespace: '{namespace}'
    deployment: '{deployment}'
  purpose: running image against Git
resource_types:
- Deployment
hypotheses:
- statement: Someone changed the cluster directly.
  supports: Live differs from the desired revision.
  refutes: Argo synced at this revision.
remediation:
- when: recent_rollout
  action: open_pull_request
  note: Make Git match the intended state rather than editing the live object.
---
# GitOps drift

MOKS has no Argo CD connector yet, so it compares what Git and the cluster say. It never guesses a revision: without a SHA in the image tag or annotations it reports that as missing.
