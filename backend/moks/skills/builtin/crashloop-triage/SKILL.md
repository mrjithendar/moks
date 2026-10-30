---
name: crashloop-triage
description: "Diagnose pods in CrashLoopBackOff by reading the crashed container's output, its configuration and the latest rollout."
triggers:
  keywords: [crash, crashloop, crashing, restarting, restart loop, CrashLoopBackOff, exit code]
  findings: [crashloop, recent_restarts, config_error]
steps:
  - tool: k8s_pod_logs
    for_each: finding.crashloop
    limit: 2
    args: {namespace: "{namespace}", pod: "{pod}", previous: true, tail_lines: 150}
    purpose: output of the crashed container
  - tool: k8s_describe_pod
    for_each: finding.crashloop
    limit: 2
    args: {namespace: "{namespace}", pod: "{pod}"}
    purpose: env, envFrom, probes, last exit code
  - tool: k8s_rollout_history
    for_each: finding.crashloop
    limit: 1
    args: {namespace: "{namespace}", deployment: "{deployment}"}
    purpose: did a rollout precede the crashes?
  - tool: kb_search
    for_each: finding.crashloop
    limit: 1
    args: {query: "{workload} crash startup configuration runbook"}
    purpose: documented failure modes
remediation:
  - when: crashloop
    action: rollback_deployment
    target: "Deployment/{deployment}"
    note: Only if a rollout happened shortly before the first crash and the previous revision was healthy.
  - when: crashloop
    action: open_pull_request
    note: If the logs show an invalid configuration value, fix it in Git (manifest/ConfigMap) rather than live.
title: CrashLoopBackOff
owner: sre
category: kubernetes
version: '2'
detection:
- id: pod-crashloop
  kind: finding
  finding: crashloop
  sustained_checks: 2
  severity: sev2
  auto_investigate: true
  auto_resolve: true
  title: Pod crash-looping
evaluation:
- fixture: crashloop-config
  expect: the lab's expected finding
---
# CrashLoopBackOff triage

**Goal:** explain why the process exits, not just that it restarts.

1. Read the **previous** container's log first; the current one is often empty during back-off.
   Look for the last lines before exit: `FATAL`, `panic`, stack traces, `invalid`, `missing`, `permission denied`, `address already in use`.
2. Match the **exit code**: 1 = application error, 78 = configuration error (EX_CONFIG), 127 = command not found,
   126 = not executable, 137 = SIGKILL (often OOM — see oomkilled-memory), 139 = segfault, 143 = SIGTERM (probe kill).
3. Check **configuration inputs** in the pod description: env values, `envFrom` ConfigMaps/Secrets (names only),
   command/args, and whether a liveness probe could be killing a slow starter.
4. Compare with the **rollout history**: a new revision minutes before the first crash is a strong timing correlation,
   but only the logs can confirm the mechanism.

**Do not** claim a root cause from timing alone, and do not recommend restarting a crash-looping pod: it will crash again.
