---
name: authn-vs-authz
title: Authentication or authorization
version: '1'
owner: sre
category: support
description: Separate 401 from 403 and send the problem to the right owner.
triggers:
  keywords:
  - '401'
  - '403'
  - unauthorized
  - forbidden
  - login failed
  - permission denied
  findings: []
steps:
- tool: loki_logs
  args:
    contains: unauthorized
    limit: 100
  purpose: log lines with unauthorized
- tool: loki_logs
  args:
    contains: forbidden
    limit: 100
  purpose: log lines with forbidden
- tool: kb_search
  args:
    query: authentication login
  purpose: documented runbooks
resource_types:
- Application service
hypotheses:
- statement: The identity is unknown (401).
  supports: Expired or missing token.
  refutes: User recognised.
- statement: The identity lacks permission (403).
  supports: User known, action refused.
  refutes: Others in the same role fail too.
---
# Authentication or authorization

401 means we do not know who you are. 403 means we do, and the answer is no. They have different owners.
