---
name: memory-growth
title: Memory growth
version: '1'
owner: sre
category: application
description: Tell a leak from normal cache growth before it becomes an OOM kill.
triggers:
  keywords:
  - memory leak
  - memory growth
  - memory increasing
  - heap
  findings:
  - memory_near_limit
steps:
- tool: prom_pod_resources
  args:
    namespace: '{namespace}'
  purpose: working set against limits
resource_types:
- Pod
- Container
telemetry:
- container_memory_working_set_bytes
hypotheses:
- statement: The process leaks memory.
  supports: Working set climbs across restarts at a steady slope.
  refutes: It plateaus.
remediation:
- when: memory_near_limit
  action: patch_resources
  target: Deployment/{deployment}
  note: Buys time. It does not fix a leak.
---
# Memory growth

Use the working set, not RSS or cache-inclusive numbers. A rising line that resets at each restart is the classic leak shape.
