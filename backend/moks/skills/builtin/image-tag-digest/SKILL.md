---
name: image-tag-digest
title: Incorrect image tag or digest
version: '1'
owner: sre
category: devops
description: Check that the running image is the intended one, and flag mutable tags.
triggers:
  keywords:
  - image tag
  - digest
  - wrong version
  - latest tag
  - wrong image
  findings:
  - mutable_image_tag
  - image_pull
steps:
- tool: k8s_config_audit
  args:
    namespace: '{namespace}'
  purpose: configuration and references
- tool: k8s_workloads
  args:
    namespace: '{namespace}'
  purpose: workload status
resource_types:
- Deployment
- Pod
hypotheses:
- statement: A mutable tag moved.
  supports: Same tag, different image ID across pods.
  refutes: Digest pinned.
---
# Incorrect image tag or digest

With :latest two pods can run different code. Pods report the resolved imageID, so compare that, not the tag.
