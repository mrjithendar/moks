---
name: request-trace-investigation
title: Request ID and trace ID investigation
version: '1'
owner: sre
category: support
description: Follow one failing request across services.
triggers:
  keywords:
  - request id
  - correlation id
  - x-request-id
  - failed request
  findings: []
steps:
- tool: loki_logs
  args:
    contains: error
    limit: 100
  purpose: log lines with error
- tool: kb_search
  args:
    query: request tracing
  purpose: documented runbooks
resource_types:
- Application service
evidence_required:
- the request or trace ID
- the time of the failure
---
# Request ID and trace ID investigation

Ask for the ID and the time. One ID across services is far cheaper than broad log searches.
