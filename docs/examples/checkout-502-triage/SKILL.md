---
name: checkout-502-triage
version: '1'
title: Checkout 502 triage
description: "Explain 502 responses from checkout by reading its Service, its pods and recent rollouts, in that order."
owner: payments-sre
category: application
environments: [prod, staging]
permissions: [investigation.run]
resource_types: [Service, Deployment]
triggers:
  keywords: [checkout 502, checkout gateway]
  findings: [service_selector_mismatch, service_no_ready_endpoints]
steps:
  - tool: k8s_services
    args: {namespace: "{namespace}"}
    purpose: does checkout have ready endpoints?
    required: true
  - tool: k8s_workloads
    args: {namespace: "{namespace}"}
    purpose: is the Deployment fully rolled out?
    required: true
  - tool: k8s_rollout_history
    for_each: finding.service_no_ready_endpoints
    limit: 1
    args: {namespace: "{namespace}", deployment: "{deployment}"}
    purpose: did a rollout precede the 502s?
  - tool: kb_search
    args: {query: "checkout 502 runbook"}
    purpose: documented failure modes, which still need live evidence
evidence_required:
  - {tool: k8s_services, label: Service and endpoint state}
  - {tool: k8s_workloads, label: Deployment rollout state}
  - "Traffic graphs for the same window, if a dashboard exists"
verification:
  - {finding: service_selector_mismatch, expect: present, label: the selector fault was observed directly}
recovery_validation:
  - Ready endpoints above zero for 15 minutes after the change
remediation:
  - when: service_selector_mismatch
    action: apply_config_patch
    target: "Service/{service}"
    note: Propose the corrected selector. It stays a plan until reviewed and approved.
evaluation:
  - fixture: broken-selector
    expect:
      selected: true
      tools: [k8s_services, k8s_workloads]
      note: selector fault found before any change is proposed
---
# Checkout 502 triage

Work from the outside in: Service, then pods, then the last rollout. A retrieved runbook tells you what
someone wrote down. It does not tell you what is true now.
