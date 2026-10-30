---
name: oomkilled-memory
description: "Diagnose OOMKilled containers by comparing memory working set with the limit and checking recent changes."
triggers:
  keywords: [oom, oomkilled, out of memory, memory, exit 137]
  findings: [oomkilled, oomkilled_earlier, event_OOMKilling, memory_near_limit]
steps:
  - tool: k8s_describe_pod
    for_each: finding.oomkilled
    limit: 2
    args: {namespace: "{namespace}", pod: "{pod}"}
    purpose: memory requests and limits
  - tool: prom_pod_resources
    for_each: finding.oomkilled
    limit: 2
    args: {namespace: "{namespace}", pod_regex: "{pod}"}
    purpose: working set versus limit over time
  - tool: k8s_rollout_history
    for_each: finding.oomkilled
    limit: 1
    args: {namespace: "{namespace}", deployment: "{deployment}"}
    purpose: did the limit or image change recently?
  - tool: finops_rightsizing
    args: {namespace: "{namespace}", days: 7}
    purpose: containers near their memory limit (OOM risk) and sized recommendations
  - tool: prom_pod_resources
    args: {namespace: "{namespace}"}
    when_question: "limit|close|near|risk|headroom"
    purpose: working set versus limit for every pod in the namespace
remediation:
  - when: memory_near_limit
    action: patch_resources
    note: Raise the memory limit/request to the rightsizing recommendation before it OOMs; validate the rollout.
  - when: oomkilled
    action: patch_resources
    target: "Deployment/{deployment}"
    note: Raise the memory limit to observed peak + headroom (see finops_rightsizing), or fix a leak if usage grows without bound.
---
# OOMKilled triage

- **Container OOM vs node OOM:** `OOMKilled` with exit 137 on the container means it hit *its own* limit. Node-level
  pressure shows as `MemoryPressure` on the node and `Evicted` pods instead (see node-pressure-evictions).
- Plot `container_memory_working_set_bytes` against `container_spec_memory_limit_bytes`:
  - a **sawtooth reaching the limit** = leak or unbounded cache;
  - a **step change after a rollout** = new code/config uses more memory;
  - **spikes at load** = limit too tight for peak traffic.
- JVM/Node/Python runtimes often need heap settings aligned with the container limit.
- A single OOM long ago (`oomkilled_earlier`) is context, not the current incident.

Recommend a limit from measured peak with ~25% headroom, never "unlimited".
