---
name: latency-regression
description: "Correlate increased latency with rollouts, dependency calls, resource throttling and application logs."
triggers:
  keywords: [latency, slow, slowness, response time, p95, p99, timeout, degraded, regression]
  findings: [recent_rollout]
steps:
  - tool: k8s_workloads
    args: {namespace: "{namespace}"}
    purpose: which deployments changed revision
  - tool: prom_pod_resources
    args: {namespace: "{namespace}"}
    purpose: CPU throttling and memory around the slowdown
  - tool: loki_search
    args: {namespace: "{namespace}", contains: "timeout", limit: 80}
    purpose: timeouts and slow dependency calls
  - tool: kb_search
    args: {query: "latency {question}"}
    purpose: known dependencies and SLOs
---
# Latency regression

1. **When did it start?** Establish the onset from metrics or log `durationMs` values, not from the alert time.
2. **What changed just before?** Rollouts (ReplicaSet creation time) of the slow service *and its dependencies*.
   A dependency's rollout shortly before onset is a candidate; say "correlates with", not "caused".
3. **Saturation:** CPU throttling ratio > 25%, memory near limit, or pods not ready reduce capacity and raise latency.
4. **Dependencies:** log lines naming a dependency with growing durations point at that dependency.
5. Verify by comparing durations before/after the candidate change.

Rollback of the suspected dependency is a reasonable mitigation once correlation is shown and the previous revision was healthy.
