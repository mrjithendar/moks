---
name: tls-chain-expiry
title: TLS chain and expiry
version: '1'
owner: sre
category: support
description: 'Diagnose TLS errors: expired certificates, wrong hostnames and missing intermediates.'
triggers:
  keywords:
  - ssl
  - certificate error
  - handshake
  - tls error
  - chain
  findings: []
steps:
- tool: k8s_ingresses
  args:
    namespace: '{namespace}'
  purpose: ingress routes
- tool: loki_logs
  args:
    contains: handshake
    limit: 100
  purpose: log lines with handshake
- tool: kb_search
  args:
    query: tls certificate
  purpose: documented runbooks
resource_types:
- Ingress
hypotheses:
- statement: An intermediate is missing.
  supports: Fails in some clients only.
  refutes: All clients fail.
---
# TLS chain and expiry

Browsers can hide a missing intermediate that strict clients reject. Use the tls_check diagnostic for what the server really sends.
