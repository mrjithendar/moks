---
name: dns-troubleshooting
title: DNS troubleshooting
version: '1'
owner: sre
category: support
description: 'Work through a user''s DNS problem: which resolver, which name, what answer.'
triggers:
  keywords:
  - nxdomain
  - dns lookup
  - resolve
  - cannot resolve
  - wrong ip
  findings: []
steps:
- tool: k8s_dns_health
  args: {}
  purpose: CoreDNS health
- tool: kb_search
  args:
    query: dns records
  purpose: documented runbooks
resource_types:
- Service
hypotheses:
- statement: Only some resolvers are wrong.
  supports: Answers differ by resolver.
  refutes: All agree.
---
# DNS troubleshooting

Ask which resolver gave the answer. Cached NXDOMAIN lasts as long as the zone's negative TTL.
