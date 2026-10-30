---
name: image-pull-failures
description: "Diagnose ErrImagePull/ImagePullBackOff: wrong reference, missing tag, or registry credentials."
triggers:
  keywords: [imagepullbackoff, errimagepull, image pull, pull access denied, manifest unknown, registry]
  findings: [image_pull]
steps:
  - tool: k8s_describe_pod
    for_each: finding.image_pull
    limit: 2
    args: {namespace: "{namespace}", pod: "{pod}"}
    purpose: exact image reference
  - tool: k8s_events
    args: {namespace: "{namespace}", warnings_only: true}
    purpose: the registry's error message
  - tool: k8s_rollout_history
    for_each: finding.image_pull
    limit: 1
    args: {namespace: "{namespace}", deployment: "{deployment}"}
    purpose: which revision introduced the image
remediation:
  - when: image_pull
    action: rollback_deployment
    target: "Deployment/{deployment}"
    note: Roll back to the last revision whose image pulled successfully while the reference is fixed.
title: Image pull failures
owner: sre
category: kubernetes
version: '2'
detection:
- id: pod-image-pull
  kind: finding
  finding: image_pull
  sustained_checks: 2
  severity: sev3
  auto_resolve: true
  title: Image cannot be pulled
---
# Image pull failures

Read the event message verbatim:
- `manifest unknown` / `not found` → tag or digest does not exist (CI did not push it, or a mutable tag was deleted).
- `pull access denied` / `no basic auth credentials` / 401/403 → imagePullSecret missing or expired
  (on mk4 the `ecr-pull` secret is refreshed by the ecr-refresh CronJob; check that the namespace is in its target list).
- `toomanyrequests` → registry rate limit.
- `no match for platform` → image not built for the node architecture (mk4 is arm64).
