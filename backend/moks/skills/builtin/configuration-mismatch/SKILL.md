---
name: configuration-mismatch
title: Configuration mismatch
version: '1'
owner: sre
category: devops
description: Find workloads whose configuration differs from what they expect, without reading Secret values.
triggers:
  keywords:
  - configuration
  - config mismatch
  - configmap
  - environment variable
  - wrong config
  findings:
  - config_error
  - config_ref_missing
steps:
- tool: k8s_config_audit
  args:
    namespace: '{namespace}'
  purpose: configuration and references
- tool: k8s_events
  args:
    namespace: '{namespace}'
  purpose: warning events
resource_types:
- Deployment
- Pod
hypotheses:
- statement: A key is absent or misnamed.
  supports: CreateContainerConfigError names it.
  refutes: All references resolve.
evaluation:
- fixture: crashloop-config
  expect:
    selected: true
    note: 'invalid PAYMENT_MODE found in previous logs'
remediation:
- when: config_ref_missing
  action: open_pull_request
  note: Add the missing reference or object in Git.
---
# Configuration mismatch

ConfigMap changes do not restart pods. A correct ConfigMap with old behaviour usually needs a rollout.
