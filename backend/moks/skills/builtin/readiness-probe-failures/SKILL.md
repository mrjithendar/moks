---
name: readiness-probe-failures
description: "Explain pods that run but are not Ready: failing readiness probes, slow start, dependency checks."
triggers:
  keywords: [not ready, readiness, probe, unhealthy, health check, 0/1]
  findings: [not_ready, event_Unhealthy, service_no_ready_endpoints]
steps:
  - tool: k8s_describe_pod
    for_each: finding.not_ready
    limit: 2
    args: {namespace: "{namespace}", pod: "{pod}"}
    purpose: probe definitions and conditions
  - tool: k8s_events
    args: {namespace: "{namespace}", warnings_only: true}
    purpose: Unhealthy events with the probe's error
  - tool: k8s_pod_logs
    for_each: finding.not_ready
    limit: 1
    args: {namespace: "{namespace}", pod: "{pod}", tail_lines: 120}
    purpose: application view of its health
title: Readiness and liveness probes
owner: sre
category: application
version: '2'
detection:
- id: pod-not-ready
  kind: finding
  finding: not_ready
  sustained_checks: 4
  severity: sev3
  auto_resolve: true
  title: Pod not Ready
---
# Readiness failures

- The `Unhealthy` event quotes the probe failure (`connection refused`, `HTTP 500`, `timeout`): start there.
- `connection refused` → wrong port or the app is not listening yet (startup probe or initialDelaySeconds too short).
- `HTTP 5xx` from the health endpoint → the app reports a failing dependency; follow that dependency.
- Probes that call downstream services make outages cascade; readiness should reflect the pod, not the world.
