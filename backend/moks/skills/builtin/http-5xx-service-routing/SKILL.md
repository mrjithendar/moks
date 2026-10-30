---
name: http-5xx-service-routing
description: "Explain 502/503/504 responses by checking the path ingress -> service -> endpoints -> ready pods."
triggers:
  keywords: ["502", "503", "504", 5xx, bad gateway, gateway timeout, service unavailable, no endpoints, upstream]
  findings: [service_selector_mismatch, service_no_ready_endpoints, service_target_port, ingress_backend_missing, not_ready]
steps:
  - tool: k8s_services
    args: {namespace: "{namespace}"}
    purpose: selectors, ports and ready endpoints
  - tool: k8s_ingresses
    args: {namespace: "{namespace}"}
    purpose: which service each route sends traffic to
  - tool: k8s_workloads
    args: {namespace: "{namespace}"}
    purpose: pod labels the selector should match
  - tool: loki_search
    args: {namespace: "{namespace}", contains: "502", limit: 50}
    purpose: where the 5xx are logged
  - tool: kb_search
    args: {query: "{question}"}
    purpose: runbooks for this service
remediation:
  - when: service_selector_mismatch
    action: apply_config_patch
    target: "Service/{service}"
    note: Patch spec.selector to the labels the intended pods actually carry — after confirming which pods are intended.
  - when: service_no_ready_endpoints
    action: rollback_deployment
    note: If pods became unready after a rollout, roll back; otherwise fix readiness first.
title: Missing Service endpoints
owner: sre
category: kubernetes
version: '2'
detection:
- id: service-selector-mismatch
  kind: finding
  finding: service_selector_mismatch
  sustained_checks: 2
  severity: sev2
  auto_investigate: true
  auto_resolve: true
  title: Service selects no pods
- id: service-no-endpoints
  kind: finding
  finding: service_no_ready_endpoints
  sustained_checks: 2
  severity: sev2
  auto_resolve: true
  title: Service has no ready endpoints
evaluation:
- fixture: broken-selector
  expect: the lab's expected finding
---
# 5xx routing triage

Walk the request path in order and stop at the first broken hop:

1. **Ingress/route** points at an existing Service and port (`ingress_backend_missing` otherwise → 404/503 from the proxy).
2. **Service selector** matches pods (`service_selector_mismatch`: 0 matching pods → no endpoints → proxies answer 502/503).
   Near-miss pods (same label keys, different values) usually reveal a typo or a renamed label.
3. **Endpoints are ready**: pods match but fail readiness → `service_no_ready_endpoints`.
4. **targetPort** exists on the pods (named ports must be declared).
5. Only then look inside the application (logs with status 5xx, upstream timeouts).

502 = the proxy got a bad/no response; 503 = no healthy upstream; 504 = upstream too slow.
