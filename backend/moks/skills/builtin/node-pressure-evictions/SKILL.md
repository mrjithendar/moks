---
name: node-pressure-evictions
description: "Diagnose evictions and node-level problems: memory/disk/PID pressure, NotReady or cordoned nodes."
triggers:
  keywords: [evicted, eviction, node pressure, diskpressure, memorypressure, notready, node down]
  findings: [evicted, event_Evicted, event_NodeNotReady]
steps:
  - tool: k8s_nodes
    purpose: node readiness, pressure conditions and taints
  - tool: k8s_events
    args: {namespace: "{namespace}", warnings_only: true}
    purpose: eviction messages naming the starved resource
  - tool: prom_targets
    purpose: kubelet/cAdvisor scrape health
---
# Node pressure and evictions

- `Evicted` pods name the starved resource (`ephemeral-storage`, `memory`); match it to the node's pressure condition.
- Memory pressure evicts BestEffort, then Burstable pods exceeding requests — set requests to protect critical pods.
- Disk pressure on Minikube usually means images/logs filling the node; prune images or grow the disk.
- NotReady nodes: kubelet down or network partition; workloads reschedule only if replicas > 1 and PDBs allow.
