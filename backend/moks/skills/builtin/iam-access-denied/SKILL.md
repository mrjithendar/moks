---
name: iam-access-denied
title: IAM access denied
version: '1'
owner: sre
category: aws
description: Investigate AccessDenied from AWS APIs and find the missing permission.
triggers:
  keywords:
  - accessdenied
  - access denied
  - iam
  - not authorized
  - assumerole
  findings: []
steps:
- tool: loki_logs
  args:
    contains: AccessDenied
    limit: 100
  purpose: log lines with AccessDenied
- tool: kb_search
  args:
    query: iam policy role
  purpose: documented runbooks
resource_types:
- EKS
- Lambda
- EC2
requires:
- cloudwatch
hypotheses:
- statement: The role lacks the action.
  supports: Error names the action and resource.
  refutes: Policy allows it; look for an SCP or boundary.
---
# IAM access denied

The message names the principal, action and resource. Check explicit denies, permission boundaries and SCPs before widening a policy. Fix in Terraform.
