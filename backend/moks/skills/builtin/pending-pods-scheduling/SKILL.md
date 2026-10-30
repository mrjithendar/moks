---
name: pending-pods-scheduling
description: "Explain Pending pods: insufficient resources, taints, affinity/selectors, or unbound volumes."
triggers:
  keywords: [pending, unschedulable, scheduling, FailedScheduling, insufficient cpu, insufficient memory, taint]
  findings: [unschedulable, pending, event_FailedScheduling, event_FailedMount, event_FailedAttachVolume]
steps:
  - tool: k8s_describe_pod
    for_each: finding.unschedulable
    limit: 2
    args: {namespace: "{namespace}", pod: "{pod}"}
    purpose: scheduler message, requests, node selectors
  - tool: k8s_events
    args: {namespace: "{namespace}", warnings_only: true}
    purpose: FailedScheduling / FailedMount details
  - tool: k8s_nodes
    purpose: allocatable capacity, pressure and taints
title: Pending pods and scheduling
owner: sre
category: kubernetes
version: '2'
detection:
- id: pod-unschedulable
  kind: finding
  finding: unschedulable
  sustained_checks: 3
  severity: sev3
  auto_resolve: true
  title: Pod cannot be scheduled
---
# Pending pods

The scheduler's message says which predicate failed — quote it:
- `Insufficient cpu/memory` → requests exceed free allocatable on every node; compare with node capacity (requests, not usage, count).
- `node(s) had untolerated taint` → missing toleration or a cordoned/tainted node.
- `didn't match Pod's node affinity/selector` → labels/affinity mismatch.
- `unbound immediate PersistentVolumeClaims` / FailedMount → storage problem (StorageClass, zone, hostPath node pinning on Minikube).

Remediation is usually a request change or capacity, not a restart.
