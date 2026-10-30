---
name: certificate-expiry
title: Certificate expiration
version: '1'
owner: sre
category: kubernetes
description: Find certificates close to expiry or failing to renew.
triggers:
  keywords:
  - certificate
  - cert-manager
  - expired
  - expiry
  - x509
  - tls
  findings: []
steps:
- tool: prom_query
  args:
    promql: (certmanager_certificate_expiration_timestamp_seconds - time()) / 86400 < 30
    mode: instant
  purpose: certificates expiring within 30 days
- tool: k8s_events
  args:
    namespace: '{namespace}'
  purpose: warning events
- tool: loki_logs
  args:
    contains: x509
    limit: 100
  purpose: log lines with x509
resource_types:
- Ingress
- Service
telemetry:
- certmanager_certificate_expiration_timestamp_seconds
queries:
  days_left:
    language: promql
    query: (certmanager_certificate_expiration_timestamp_seconds - time()) / 86400
    purpose: days until expiry
hypotheses:
- statement: Renewal is failing.
  supports: Certificate not Ready; ACME challenge errors.
  refutes: Renewal scheduled and Ready.
recovery_validation:
- certificate Ready with a later expiry
detection:
- id: certificate-expiring
  kind: prometheus
  query: (certmanager_certificate_expiration_timestamp_seconds - time()) / 86400
  operator: <
  threshold: 14
  resource_label:
  - namespace
  - name
  interval_minutes: 60
  sustained_checks: 1
  severity: sev2
  auto_resolve: true
  title: Certificate expires within 14 days
---
# Certificate expiration

cert-manager renews well before expiry, so a certificate this close usually means renewal already failed. Read the Certificate, CertificateRequest and Order events.
