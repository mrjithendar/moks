---
name: deployment-drift-git
description: "Compare what is running with Git: deployed commit, undeployed changes and recent commits touching the service."
triggers:
  keywords: [git, commit, drift, deployed version, which version, revision, changed, release, configuration change]
  findings: [recent_rollout]
steps:
  - tool: k8s_workloads
    args: {namespace: "{namespace}"}
    purpose: images and revisions running now
  - tool: k8s_rollout_history
    for_each: finding.recent_rollout
    limit: 2
    args: {namespace: "{namespace}", deployment: "{deployment}"}
    purpose: what changed between revisions
title: Deployment drift
owner: sre
category: devops
version: '2'
---
# Deployment drift against Git

- Tie a Deployment to a commit **only** when the image tag or a revision annotation carries a commit SHA
  (`git_compare_deployment` reports "cannot be determined" otherwise — repeat that, do not guess from `:latest`).
- When resolved, list commits on the default branch that are **not deployed** and commits that touched the
  service's manifests recently (`git_recent_commits` with a path).
- Configuration drift (live differs from Git) should be fixed through Git (open_pull_request), not by patching live.
