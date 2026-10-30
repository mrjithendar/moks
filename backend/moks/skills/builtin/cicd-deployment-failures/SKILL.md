---
name: cicd-deployment-failures
title: CI/CD deployment failures
version: '1'
owner: sre
category: devops
description: Work out why a pipeline's change did or did not reach the cluster.
triggers:
  keywords:
  - pipeline
  - ci failed
  - build failed
  - deploy failed
  - github actions
  findings: []
steps:
- tool: k8s_workloads
  args:
    namespace: '{namespace}'
  purpose: workload status
- tool: kb_search
  args:
    query: ci pipeline deploy
  purpose: documented runbooks
resource_types:
- Deployment
requires:
- git
hypotheses:
- statement: The image never reached the registry.
  supports: ImagePullBackOff with not found.
  refutes: Tag exists.
---
# CI/CD deployment failures

Follow the change: commit, build, image, registry, manifest, cluster. Find the last step that happened.
