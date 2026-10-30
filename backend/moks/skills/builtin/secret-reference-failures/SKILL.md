---
name: secret-reference-failures
title: Secret reference failures
version: '1'
owner: sre
category: devops
description: Find missing Secret references. Secret values are never read.
triggers:
  keywords:
  - secret
  - secretkeyref
  - missing secret
  - secret not found
  findings:
  - config_ref_missing
  - plaintext_secret_env
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
- Pod
- Deployment
hypotheses:
- statement: The Secret or key does not exist in this namespace.
  supports: Event names it.
  refutes: Pod started with it.
detection:
- id: secret-reference-missing
  kind: finding
  finding: config_ref_missing
  sustained_checks: 2
  severity: sev2
  auto_resolve: true
  title: Missing ConfigMap or Secret reference
remediation:
- when: config_ref_missing
  action: open_pull_request
  note: Fix the reference in Git. Create the Secret through your secrets process, never through MOKS.
---
# Secret reference failures

MOKS reports names only. It never reads Secret contents, and does not need to for this.
