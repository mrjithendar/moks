---
name: user-outage-triage
title: User-reported outage triage
version: '1'
owner: sre
category: support
description: 'Turn ''it''s down'' into scope: who, what, since when, and what changed.'
triggers:
  keywords:
  - outage
  - site down
  - down for
  - not working
  - users report
  - cannot login
  findings: []
steps:
- tool: k8s_triage
  args:
    namespace: '{namespace}'
  purpose: pods, services and workloads
- tool: k8s_ingresses
  args:
    namespace: '{namespace}'
  purpose: ingress routes
- tool: prom_alerts
  args:
    firing_only: true
  purpose: what is already firing
resource_types:
- Application service
- Ingress
evidence_required:
- who is affected
- when it started
- what they tried
---
# User-reported outage triage

Scope first. One user, one region or everyone point in different directions. Treat an unconfirmed report as a report: state what you could and could not reproduce.
