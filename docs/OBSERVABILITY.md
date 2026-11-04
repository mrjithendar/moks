# Observability: MOKS's own logs, metrics and traces

This page is about telemetry **MOKS produces about itself**: is the portal up, why was an investigation slow,
did a model answer get used. It is not about the Prometheus, Loki and Kubernetes data MOKS *reads* during an
investigation through its connectors. On mk4 both live in the same Prometheus and Loki, so the difference is the
data, not the backend: MOKS's own lines are `{namespace="moks"}`, its own metrics are `job="moks"`.

There is no Langfuse or LangSmith for MOKS. LangSmith is forced off in `backend/moks/llm/lc.py` because it would
send prompts holding redacted evidence to a third party. The Langfuse Cloud set-up in the tools repository
(`deploy/minikube/observability/langfuse`) belongs to RoboShop's unrelated "Moks" shopping widget.

## Where each signal goes (mk4)

| Signal | Produced by | Collected by | Stored in | Read with |
| --- | --- | --- | --- | --- |
| JSON logs on stdout | `observability.JsonFormatter`, every process | Alloy (`monitoring`), allowlisted fields only | Loki | Grafana Explore, `kubectl logs` for the full line |
| Traces | OTel SDK, OTLP/HTTP to `OTEL_EXPORTER_OTLP_ENDPOINT` | OpenTelemetry Collector (`observability`), strips URLs, SQL and exception text | Jaeger 2.22, in memory | Jaeger UI (port-forward), Jaeger API `/api/v3` |
| Metrics | `prometheus_client` on `:9464` | Prometheus job `moks` through headless Service `moks-metrics` | Prometheus, 7 days | Grafana folder MOKS, Prometheus alerts |

Service names: `moks` (API), `moks-worker`, `moks-executor`, `moks-terminal`. Each line also carries `service` and
`version` (package version plus commit), but Alloy forwards only its allowlist, so those two are in
`kubectl logs`, not Loki. In Loki, the pod and container labels say which process wrote a line.

## Correlation

- **The trace id is the join key.** Alloy keeps `trace_id` in every forwarded line; Jaeger indexes traces by it.
  When tracing is on, the API's request id is the trace id unless the caller sent its own plain `X-Request-ID`;
  responses carry both `X-Request-ID` and `X-Trace-ID`.
- **An investigation keeps the trace of the request that queued it.** The API stores a W3C `traceparent` with the
  investigation's first event; the worker continues it, so request, run, LangGraph nodes, tools and model calls are
  one trace. `GET /moks/api/investigations/{id}` returns it as `trace_id`, and the investigation page shows it once
  the run has finished.
- **Queued jobs** carry `_traceparent` in their payload and run as `job <type>` spans under the request that queued them.
- Request, user, job, investigation and plan ids are on every log line written inside that unit of work
  (`observability.bound`). They are never labels: Loki labels are namespace, pod, container, service_name and level.

## Span names

| Span | Where | Attributes worth filtering on |
| --- | --- | --- |
| `GET /moks/api/...` | FastAPI instrumentation | `http.route`; event streams add `moks.stream.outcome` (end, timeout, revoked, disconnected, cancelled) and `moks.stream.events` |
| `investigation` | worker | `moks.investigation.id`, `moks.investigation.status`, `moks.mode`, `moks.intent`, `moks.workflow` |
| `node <name>` | each LangGraph step | `moks.node`; `node report` gets `moks.llm.answer_unused` when the model's answer was not used |
| `tool <name>` | ToolExecutor | `moks.tool`, `moks.dependency`, `moks.outcome` (ok or the error kind) |
| `llm.complete` | every chat call | `gen_ai.system`, `gen_ai.request.model`, `gen_ai.response.model`, token counts, `moks.llm.retries`, `moks.llm.fallback`, `moks.llm.adjusted` |
| `llm.embed` | every embedding call | `moks.purpose` (search, indexing, ...), `moks.embed.texts` |
| `rag.search` | knowledge search | `moks.rag.mode`, `moks.rag.results`, `moks.rag.considered`, `moks.rag.abstain`, `moks.rag.fallback`, `moks.rag.rerank` |
| `job <type>` | job runner | `moks.job.type`, `moks.job.attempt` |
| `db SELECT` ... | SQLAlchemy, only inside a recording span | verb only, never the statement |
| `GET <host>` | httpx, only inside a recording span | host only, never the path or query |

Failures are recorded by `error.type` and `moks.error_code`, never by exception message. Prompts, model output,
retrieved text, tool output and queries are not put on spans or log lines.

The Anthropic SDK (2.x) uses `httpx2`, which the httpx instrumentation does not see: Claude calls have an
`llm.complete` span but no HTTP child spans, and the SDK's own retries are not counted.

## Sampling and limits

- `config.traceSampleRatio` (env `MOKS_TRACE_SAMPLE_RATIO`, default 1) is the share of new traces kept.
  Children always follow their parent, so a kept investigation is kept whole.
- An outgoing HTTP call with no parent span is never sampled. Background probes and outbox flushes used to make
  one-span traces: 150 of the 200 most recent `moks-worker` traces on mk4 on 2026-11-04.
- Event-stream polling is not recorded: the stream polls twice a second for up to ten minutes, and each poll used
  to add SQL spans under the request span (14 in a 1.2 s test stream).
- The exporter queue is bounded (2048 spans, 5 s export timeout). When the collector is slow or gone, spans are
  dropped and requests carry on (`tests/test_telemetry_boundaries.py`).

## Useful queries

Loki (lines are logfmt after Alloy):

```logql
{namespace="moks"} |= "trace_id=<32 hex>"                      # every line of one request or run
{namespace="moks", level=~"warn|error"}                         # what went wrong lately
{namespace="moks"} | logfmt | event="llm_answer_unused"         # model answers MOKS could not use, by error_code
{namespace="moks"} | logfmt | event="tool_call" | outcome="failure"
{namespace="moks"} | logfmt | event="http_request" | status=~"5.."
```

PromQL:

```promql
sum by (stage, reason) (increase(moks_llm_answers_unused_total[1h]))
sum by (provider, outcome) (rate(moks_llm_requests_total[5m]))
histogram_quantile(0.95, sum by (le, mode) (rate(moks_investigation_duration_seconds_bucket[30m])))
sum by (tool, outcome) (rate(moks_tool_calls_total[5m]))
sum by (reason) (rate(moks_retrieval_fallbacks_total[15m]))
sum(moks_sse_streams_open)
```

Jaeger API (no ingress; read through the API server):

```bash
kubectl --context mk4 get --raw "/api/v1/namespaces/observability/services/jaeger:16686/proxy/api/v3/traces/<trace id>"
```

## Dashboards and alerts

- Dashboard: `deploy/grafana/moks-dashboard.json`, copied to the tools repository as
  `deploy/minikube/observability/grafana/dashboards/moks-overview.json` (Grafana folder MOKS). Edit it here, then copy it.
- Scrape job and alerts live in the tools repository: `prometheus/values.yaml` job `moks`, and `files/alerts.yml`
  groups `moks`, `moks-durability`, `moks-search`. `deploy/grafana/prometheus-scrape.yaml` and `moks-alerts.yaml` here
  are the reference copies.
- mk4 has no Alertmanager, so alerts show in Prometheus and Grafana and notify no one.

## Troubleshooting a slow or failed investigation

1. Open the investigation and copy its trace id (or find it in Loki with `| logfmt | event="investigation_finished"`).
2. Open the trace. Under `investigation`, the longest `node` is where the time went; `llm.complete` spans give model
   latency and tokens; a `tool` span with `moks.outcome` other than `ok` names the failing dependency.
3. If the report says it is rule-based although model calls succeeded, look for `moks.llm.answer_unused` on
   `node report`, or `event="llm_answer_unused"` in Loki. `BudgetExceeded` means the investigation's token budget
   (`max_tokens_per_investigation`, default 60 000) had no room left for the report.
4. Read the full lines with `kubectl --context mk4 -n moks logs deploy/moks-worker | grep <trace id>`; they include
   the redacted error message that Loki does not keep.
