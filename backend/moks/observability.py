"""Metrics, structured logs and traces for MOKS itself.

- Prometheus metrics on a separate port (MOKS_METRICS_PORT, default 9464), never routed by
  the ingress.
- JSON logs on stdout using the field names the cluster's Alloy pipeline keeps
  (level, event, trace_id, route, method, status, durationMs, dependency, operation, outcome,
  errorType, errorCode, logger_name). Alloy forwards only those, so the rest stay in `kubectl logs`.
- OpenTelemetry traces exported over OTLP/HTTP when OTEL_EXPORTER_OTLP_ENDPOINT is set.
  Spans carry metadata only: never prompts, model output or log contents.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time

from prometheus_client import Counter, Gauge, Histogram, start_http_server

# ---- Metrics -----------------------------------------------------------------------------

HTTP_REQUESTS = Counter("moks_http_requests_total", "API requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "moks_http_request_duration_seconds", "API latency", ["method", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
CONNECTOR_REQUESTS = Counter(
    "moks_connector_requests_total", "Connector calls", ["connector_type", "operation", "outcome"]
)
CONNECTOR_LATENCY = Histogram(
    "moks_connector_latency_seconds", "Connector call latency", ["connector_type", "operation"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
INVESTIGATIONS = Counter("moks_investigations_total", "Finished investigations", ["outcome", "mode"])
INVESTIGATION_DURATION = Histogram(
    "moks_investigation_duration_seconds", "Investigation wall time", ["mode"],
    buckets=(1, 2.5, 5, 10, 20, 30, 60, 120, 180, 300),
)
INVESTIGATIONS_RUNNING = Gauge("moks_investigations_running", "Investigations running in this worker")
TOOL_CALLS = Counter("moks_tool_calls_total", "Tool calls", ["tool", "outcome"])
POLICY_DENIALS = Counter("moks_policy_denials_total", "Authorization and policy denials", ["kind"])
LLM_REQUESTS = Counter("moks_llm_requests_total", "LLM requests", ["provider", "model", "outcome"])
LLM_LATENCY = Histogram(
    "moks_llm_latency_seconds", "LLM call latency", ["provider"],
    buckets=(0.5, 1, 2, 5, 10, 20, 40, 60, 120),
)
LLM_TOKENS = Counter("moks_llm_tokens_total", "LLM tokens", ["provider", "model", "direction"])
LLM_COST = Counter("moks_llm_cost_usd_total", "Estimated LLM cost in USD", ["provider", "model"])
RETRIEVAL_LATENCY = Histogram("moks_retrieval_latency_seconds", "RAG retrieval latency", ["backend"])
INDEXING_DOCUMENTS = Gauge("moks_indexing_documents", "Indexed documents", ["source", "state"])
ACTIONS = Counter("moks_actions_total", "Remediation actions", ["action", "outcome"])
JOBS_QUEUED = Gauge("moks_jobs_queued", "Jobs waiting to run", ["job_class"])
JOBS_OLDEST_AGE = Gauge("moks_jobs_oldest_age_seconds", "Age of the oldest job that is due", ["job_class"])
JOBS_RUNNING = Gauge("moks_jobs_running", "Jobs running across workers", ["job_class"])
JOB_DELAY = Histogram("moks_job_schedule_delay_seconds", "Time a due job waited to start", ["job_class"], buckets=(1, 5, 15, 30, 60, 120, 300, 900, 3600))
JOB_RUNTIME = Histogram("moks_job_runtime_seconds", "Job duration", ["job_class", "type"], buckets=(1, 5, 15, 60, 300, 900, 3600, 14400))
JOB_RETRIES = Counter("moks_job_retries_total", "Jobs queued again after a failure", ["type"])
JOBS_ABANDONED = Counter("moks_jobs_abandoned_total", "Jobs whose worker stopped", ["type"])
JOBS_REJECTED = Counter("moks_jobs_rejected_total", "Jobs refused because the queue is full", ["job_class"])
APPROVALS = Counter("moks_approvals_total", "Approval decisions", ["decision"])


BACKUP_LAST_SUCCESS = Gauge("moks_backup_last_success_timestamp_seconds", "When the last backup succeeded")
BACKUP_LAST_RUN_OK = Gauge("moks_backup_last_run_success", "1 if the latest backup run succeeded, otherwise 0")
DATABASE_SIZE = Gauge("moks_database_size_bytes", "Size of the MOKS database")
DATABASE_VOLUME = Gauge("moks_database_volume_bytes", "Capacity of the database volume")


def start_metrics_server(port: int) -> None:
    try:
        start_http_server(port)
    except OSError as exc:  # port already in use (e.g. embedded worker in the same process)
        logging.getLogger("moks").warning("metrics server not started", extra={"event": "metrics_port_busy", "errorType": type(exc).__name__})


# ---- Logging -----------------------------------------------------------------------------

_EXTRA_FIELDS = (
    "event", "route", "method", "status", "durationMs", "outcome", "errorType", "operation",
    "dependency", "investigation_id", "tool", "action", "decision", "connector_type", "request_id", "errorCode",
)
MAX_STACK = 4000


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "logger_name": record.name,
            "message": record.getMessage(),
        }
        for key in _EXTRA_FIELDS:
            if hasattr(record, key):
                out[key] = getattr(record, key)
        out.setdefault("event", record.getMessage().replace(" ", "_")[:60])
        try:
            from opentelemetry import trace

            ctx = trace.get_current_span().get_span_context()
            if ctx.is_valid:
                out["trace_id"] = format(ctx.trace_id, "032x")
        except Exception:  # pragma: no cover - tracing optional
            pass
        if record.exc_info:
            out["errorType"] = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
            from .redaction import redact

            out["stack"] = redact(self.formatException(record.exc_info))[-MAX_STACK:]  # the end of a traceback says the most
        return json.dumps(out, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access", "httpx", "httpx2", "httpcore", "anthropic", "botocore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.captureWarnings(True)

    def crashed(kind, value, tb):  # a crash is the line most worth having as JSON
        logging.getLogger("moks").critical("process crashed", exc_info=(kind, value, tb), extra={"event": "process_crashed"})
    sys.excepthook = crashed


# ---- Tracing -----------------------------------------------------------------------------

_tracing_ready = False


def setup_tracing(service_name: str, app=None) -> None:
    """Configure OTLP export if OTEL_EXPORTER_OTLP_ENDPOINT is set; otherwise spans are no-ops."""
    global _tracing_ready
    if _tracing_ready or not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from . import __version__
    from .config import get_settings

    provider = TracerProvider(resource=Resource.create({
        "service.name": service_name, "service.namespace": "moks", "service.version": __version__,
        "service.instance.id": os.environ.get("HOSTNAME", ""), "vcs.ref.head.revision": get_settings().build_commit}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz", exclude_spans=["send", "receive"])
    _tracing_ready = True


def tracer():
    from opentelemetry import trace

    return trace.get_tracer("moks")


def traceparent() -> str | None:
    """The current span as a W3C header value, to store with work another process will pick up."""
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier)
    return carrier.get("traceparent")


def continue_trace(value: str | None):
    """A context that makes the next span a child of the request that queued this work."""
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    return TraceContextTextMapPropagator().extract({"traceparent": value}) if value else None
