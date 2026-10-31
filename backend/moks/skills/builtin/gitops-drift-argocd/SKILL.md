---
name: gitops-drift-argocd
title: GitOps drift and Argo CD sync failures
version: '2'
owner: sre
category: devops
description: Read what Argo CD reports about each application, then compare the running workload with what Git declares.
triggers:
  keywords:
  - drift
  - argo
  - argocd
  - argo cd
  - out of sync
  - gitops
  - sync failed
  - sync fail
  - syncs fail
  findings:
  - recent_rollout
steps:
- tool: k8s_argocd_status
  purpose: sync status, health and errors Argo CD reports
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
- Application
hypotheses:
- statement: Files moved in Git and the live Application still points at the old path.
  supports: Argo CD reports that the app path or a values file does not exist, and Git has it elsewhere.
  refutes: The path exists at the revision Argo CD is reading.
- statement: The app was replaced by another one that was never deployed.
  supports: A Git path for the failing app sits under a new app's folder, and that app is missing from Argo CD.
  refutes: The replacement app already exists in Argo CD.
- statement: The project forbids something the app needs.
  supports: The sync result or a condition names a repository, destination or kind the project does not permit.
  refutes: Argo CD reports no project error.
- statement: Someone changed the cluster directly.
  supports: Live differs from the desired revision and Argo CD can compare them.
  refutes: Argo synced at this revision.
remediation:
- when: recent_rollout
  action: open_pull_request
  note: Make Git match the intended state rather than editing the live object.
---
# GitOps drift and Argo CD sync failures

Start with what Argo CD reports. Workloads keep running while a sync fails, so healthy pods say nothing about Argo CD. Quote its error, and name each failing app with the path it reads.

`Unknown` sync status means Argo CD could not build the desired state, which is different from `OutOfSync`, where it could compare and found a difference. A failing sync of this kind changes nothing in the cluster.

Paths listed as found elsewhere in Git are candidates from file names. Say which one looks right and why, but do not call it confirmed. Check for a replacement app before suggesting the old one be repointed: if an Ingress in Git already routes to the new app, changing it first can cause an outage.

Never recommend a sync with prune here: if the source is wrong, prune can delete everything the app tracks, including namespaces and volumes. MOKS cannot sync or change Argo CD.

For image drift, MOKS never guesses a revision: without a SHA in the image tag or annotations it reports that as missing.
