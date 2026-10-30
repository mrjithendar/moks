---
name: registry-auth-failures
title: Registry authentication failures
version: '1'
owner: sre
category: devops
description: Separate an expired pull credential from a missing image.
triggers:
  keywords:
  - registry
  - imagepullsecret
  - unauthorized
  - ecr
  - pull secret
  - denied
  findings:
  - image_pull
steps:
- tool: k8s_describe_pod
  for_each: finding.image_pull
  limit: 2
  args:
    namespace: '{namespace}'
    pod: '{pod}'
  purpose: exact pull error
- tool: k8s_events
  args:
    namespace: '{namespace}'
  purpose: warning events
resource_types:
- Pod
hypotheses:
- statement: The credential expired.
  supports: 401 or 'no basic auth credentials'.
  refutes: '''manifest unknown'': the tag is missing.'
---
# Registry authentication failures

ECR tokens last 12 hours, so a refresh job that stopped looks like this. 'manifest unknown' is a different problem: the tag does not exist.
