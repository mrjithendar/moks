---
name: ingress-gateway-errors
title: Ingress 502, 503 and 504
version: '1'
owner: sre
category: kubernetes
description: Trace gateway errors from the ingress to its Service and pods, and tell routing faults from slow backends.
triggers:
  keywords:
  - '502'
  - '503'
  - '504'
  - bad gateway
  - gateway timeout
  - ingress
  - traefik
  findings:
  - service_selector_mismatch
  - service_no_ready_endpoints
  - service_target_port
steps:
- tool: k8s_ingresses
  args:
    namespace: '{namespace}'
  purpose: ingress routes
- tool: k8s_services
  args:
    namespace: '{namespace}'
  purpose: selectors and endpoints
- tool: k8s_triage
  args:
    namespace: '{namespace}'
  purpose: pods, services and workloads
- tool: loki_logs
  args:
    contains: '502'
    limit: 100
  purpose: log lines with 502
resource_types:
- Ingress
- Service
hypotheses:
- statement: The Service has no ready backends (503).
  supports: 0 ready endpoints.
  refutes: Ready endpoints exist.
- statement: The backend closes or refuses connections (502).
  supports: Connection errors in gateway logs.
  refutes: Backend logs show the requests served.
- statement: The backend is too slow (504).
  supports: Duration near the gateway timeout.
  refutes: Fast responses.
recovery_validation:
- ready endpoints above zero
- gateway error rate back to baseline
evaluation:
- fixture: broken-selector
  expect: service_selector_mismatch verified with near-miss pods
remediation:
- when: service_selector_mismatch
  action: apply_config_patch
  target: Service/{service}
  note: Fix spec.selector, after confirming the near-miss pods are the intended backends.
---
# Ingress 502, 503 and 504

502, 503 and 504 mean different things. Start at the endpoints: they rule routing in or out in one check.
