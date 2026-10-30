---
name: service-reachability
title: Service reachability
version: '1'
owner: sre
category: support
description: 'Answer ''can X reach Y'' from the outside in: DNS, ingress, Service, endpoints, pod.'
triggers:
  keywords:
  - unreachable
  - cannot connect
  - connection refused
  - reachability
  - not reachable
  findings:
  - service_no_ready_endpoints
steps:
- tool: k8s_ingresses
  args:
    namespace: '{namespace}'
  purpose: ingress routes
- tool: k8s_services
  args:
    namespace: '{namespace}'
  purpose: selectors and endpoints
- tool: k8s_dns_health
  args: {}
  purpose: CoreDNS health
- tool: k8s_triage
  args:
    namespace: '{namespace}'
  purpose: pods, services and workloads
resource_types:
- Service
- Ingress
hypotheses:
- statement: A network policy blocks the path.
  supports: Endpoints ready but connections hang.
  refutes: Direct pod access works.
---
# Service reachability

Connection refused means something answered and rejected you; a hang usually means a drop. Use a Diagnostic session for an active test.
