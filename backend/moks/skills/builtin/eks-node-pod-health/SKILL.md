---
name: eks-node-pod-health
title: EKS node and pod health
version: '1'
owner: sre
category: aws
description: Read the EKS control plane, node groups and add-ons alongside pod state.
triggers:
  keywords:
  - eks
  - node group
  - nodegroup
  - add-on
  - fargate
  findings: []
steps:
- tool: eks_cluster
  args: {}
  purpose: control plane, node groups, add-ons
- tool: k8s_nodes
  args: {}
  purpose: node readiness and pressure
- tool: cw_alarms
  args:
    state: ALARM
  purpose: CloudWatch alarms in ALARM
resource_types:
- EKS
hypotheses:
- statement: A node group is degraded.
  supports: Health issues on the node group.
  refutes: All active.
---
# EKS node and pod health

Node group health issues such as Ec2SubnetInvalidConfiguration come from AWS and do not appear in pod events.
