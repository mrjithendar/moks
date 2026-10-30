---
name: rbac-denials
title: Kubernetes RBAC denial
version: '1'
owner: sre
category: kubernetes
description: 'Explain a forbidden API call: which ServiceAccount, which verb and resource, and the narrowest fix.'
triggers:
  keywords:
  - forbidden
  - rbac
  - serviceaccount
  - cannot list
  - cannot get
  - is forbidden
  findings: []
steps:
- tool: k8s_events
  args:
    namespace: '{namespace}'
  purpose: warning events
- tool: loki_logs
  args:
    contains: forbidden
    limit: 100
  purpose: log lines with forbidden
- tool: kb_search
  args:
    query: rbac permissions serviceaccount
  purpose: documented runbooks
resource_types:
- Pod
- Namespace
hypotheses:
- statement: The ServiceAccount lacks a Role binding.
  supports: '''is forbidden'' names the account, verb and resource.'
  refutes: A binding covers it.
---
# Kubernetes RBAC denial

The error text names the user, verb, resource and namespace. Grant exactly that in Git; never bind cluster-admin to fix an incident. MOKS proposes this as a pull request only.
