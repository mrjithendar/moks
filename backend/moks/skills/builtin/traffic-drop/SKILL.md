---
name: traffic-drop
title: Traffic drop
version: '1'
owner: sre
category: application
description: Investigate a sudden fall in requests, which often means failure upstream of the service.
triggers:
  keywords:
  - traffic drop
  - no traffic
  - requests dropped
  - traffic fell
  findings: []
steps:
- tool: prom_query
  args:
    promql: sum by (job) (rate(http_requests_total[5m]))
    mode: instant
  purpose: current request rate
- tool: k8s_ingresses
  args:
    namespace: '{namespace}'
  purpose: ingress routes
- tool: k8s_services
  args:
    namespace: '{namespace}'
  purpose: selectors and endpoints
resource_types:
- Application service
- Ingress
telemetry:
- http_requests_total
hypotheses:
- statement: Traffic stopped reaching the service.
  supports: Ingress or DNS change, gateway errors.
  refutes: Gateway sees normal volume.
---
# Traffic drop

Silence is not health. Compare with the same time last week before concluding anything, and check the gateway and DNS first.
