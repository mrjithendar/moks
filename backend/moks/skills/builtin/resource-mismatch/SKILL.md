---
name: resource-mismatch
title: Resource request and limit mismatch
version: '1'
owner: sre
category: kubernetes
description: 'Find workloads whose requests and limits do not match real usage: wasted capacity, or OOM and throttling risk.'
triggers:
  keywords:
  - requests
  - limits
  - rightsizing
  - over provisioned
  - under provisioned
  findings:
  - missing_requests
  - missing_memory_limit
  - over_provisioned
  - under_provisioned
  - memory_near_limit
steps:
- tool: k8s_config_audit
  args:
    namespace: '{namespace}'
  purpose: configuration and references
- tool: finops_rightsizing
  args:
    namespace: '{namespace}'
    days: 7
  purpose: usage against requests
resource_types:
- Deployment
- Container
requires:
- cost_model
evidence_required:
- p95 CPU and max memory over at least a week
remediation:
- when: under_provisioned
  action: patch_resources
  target: Deployment/{deployment}
  note: Use the recommended values, then review for a week.
---
# Resource request and limit mismatch

Size from a window that includes the busiest period. Memory limits below max usage cause OOM kills, so never trim them from an average.
