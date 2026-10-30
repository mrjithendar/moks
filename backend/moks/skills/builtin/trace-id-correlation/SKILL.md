---
name: trace-id-correlation
description: "Follow one request across services using its trace ID or request ID in logs."
triggers:
  keywords: [trace, trace id, traceid, request id, correlation id, span]
  findings: []
steps:
  - tool: loki_search
    args: {namespace: "{namespace}", trace_id: "{trace_id}", limit: 200}
    purpose: every log line carrying the trace ID
  - tool: loki_search
    args: {namespace: "{namespace}", errors_only: true, limit: 80}
    purpose: errors around the same time
---
# Trace and request correlation

- Order the matching lines by time and name the **first service that logged an error** for the trace — later errors are usually propagation.
- Note gaps: a service that should appear but does not either dropped the context or never received the call.
- Durations between hops show where time was spent.
- If Loki only keeps allowlisted fields (as on mk4), trace IDs remain in the line; search text, not labels.
