"""Metrics, structured logs and traces for MOKS itself.

- Prometheus metrics on a separate port (MOKS_METRICS_PORT, default 9464), never routed by
  the ingress.
- JSON logs on stdout using the field names the cluster's Alloy pipeline keeps
  (level, event, trace_id, route, method, status, durationMs, dependency, operation, outcome,
  errorType, errorCode, logger_name). Alloy forwards only those, so the rest stay in `kubectl logs`.
- OpenTelemetry traces exported over OTLP/HTTP when OTEL_EXPORTER_OTLP_ENDPOINT is set.
  Spans carry metadata only: never prompts, model output or log contents.
- A log context (request, user, job, investigation, plan) bound once per unit of work and
  stamped on every line written inside it, so one id finds every line of a request or a run.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

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
LLM_RETRIES = Counter("moks_llm_retries_total", "Provider calls repeated after a rate limit or overload", ["provider"])
LLM_FALLBACKS = Counter("moks_llm_fallbacks_total", "Answers served by a different model than the one asked", ["provider", "model"])
LLM_ANSWERS_UNUSED = Counter("moks_llm_answers_unused_total", "Model answers MOKS could not use, so it wrote a rule-based one",
                             ["stage", "reason"])
EMBED_REQUESTS = Counter("moks_embedding_requests_total", "Embedding calls", ["provider", "outcome"])
EMBED_LATENCY = Histogram("moks_embedding_latency_seconds", "Embedding call latency", ["provider"],
                          buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30))
SSE_STREAMS_OPEN = Gauge("moks_sse_streams_open", "Investigation event streams open in this process")
SSE_STREAMS = Counter("moks_sse_streams_total", "Investigation event streams by how they ended", ["outcome"])
RERANK_CALLS = Counter("moks_rerank_calls_total", "Reranking attempts by outcome", ["outcome"])
RERANK_SECONDS = Histogram("moks_rerank_seconds", "Round trip to the reranking service", buckets=(0.02, 0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 3.2))
RERANK_CANDIDATES = Histogram("moks_rerank_candidates", "Authorized candidates sent for reranking", buckets=(1, 5, 10, 20, 30, 40, 50))
RERANK_TRUNCATED = Counter("moks_rerank_truncated_passages_total", "Passages only partly scored")
OPENSEARCH_BULK_ITEMS = Counter("moks_opensearch_bulk_items_total", "Bulk items by what OpenSearch said about each", ["outcome"])
OPENSEARCH_SEARCH_SECONDS = Histogram("moks_opensearch_search_seconds", "OpenSearch query time", ["kind"], buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5))
OPENSEARCH_SEARCH_FAILURES = Counter("moks_opensearch_search_failures_total", "OpenSearch queries that gave no answer", ["kind", "reason"])
KNOWLEDGE_RETRIEVALS = Counter("moks_knowledge_retrievals_total", "Knowledge stages of investigations, by stage and outcome", ["stage", "outcome"])
RETRIEVAL_FALLBACKS = Counter("moks_retrieval_fallbacks_total", "Searches answered with less than was asked for", ["reason"])
RETRIEVAL_MODES = Counter("moks_retrieval_searches_total", "Searches by the mode that actually ran", ["mode"])
SEARCH_BACKEND = Gauge("moks_search_backend", "Search backend state, from the worker's probe", ["cluster", "metric"])
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


_UNUSABLE = (("no JSON object", "no_json"), ("not a JSON object", "not_object"), ("schema invalid", "schema_invalid"))


def llm_answer_unused(stage: str, exc: BaseException) -> str:
    """Records why a model's answer was not used (stage: report, guidance, concept). The model call itself may
    have succeeded: an answer that is not JSON or fails the schema ends here too, and before this only the
    report's notes said so. The reason is a fixed code, never the model's text."""
    kind = getattr(exc, "kind", None)
    reason = kind if isinstance(kind, str) and kind else next((code for words, code in _UNUSABLE if words in str(exc)), type(exc).__name__)
    reason = reason[:40]
    LLM_ANSWERS_UNUSED.labels(stage, reason).inc()
    try:
        from opentelemetry import trace

        here = trace.get_current_span()
        here.set_attribute("moks.llm.answer_unused", reason)
    except Exception:  # pragma: no cover - tracing optional
        pass
    logging.getLogger("moks.llm").warning("model answer not used", extra={"event": "llm_answer_unused", "operation": stage,
                                                                          "errorType": type(exc).__name__, "errorCode": reason})
    return reason


# ---- Log context -------------------------------------------------------------------------

# Bound per request (middleware), per job, per investigation and per action plan. asyncio
# tasks copy it when created, so work started inside a unit keeps that unit's ids.
_log_context: ContextVar[dict[str, Any]] = ContextVar("moks_log_context", default={})


@contextmanager
def bound(**fields: Any) -> Iterator[None]:
    """Adds fields (None values skipped) to every log line written inside the block."""
    token = _log_context.set({**_log_context.get(), **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _log_context.reset(token)


def bind(**fields: Any) -> None:
    """Adds fields for the rest of the current context, e.g. the user once a request is authenticated."""
    _log_context.set({**_log_context.get(), **{k: v for k, v in fields.items() if v is not None}})


def log_context() -> dict[str, Any]:
    return dict(_log_context.get())


def current_trace_ids() -> tuple[str | None, str | None]:
    """(trace_id, span_id) of the current span as hex, or (None, None) when tracing is off."""
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
    except Exception:  # pragma: no cover - tracing optional
        pass
    return None, None


MAX_ERROR = 500


def error_fields(exc: BaseException) -> dict[str, str]:
    """errorType, errorCode (a ConnectorError/LLMError kind) and a redacted, shortened message.
    Only errorType and errorCode pass the Alloy allowlist; the message stays in `kubectl logs`."""
    from .redaction import redact

    out = {"errorType": type(exc).__name__, "error": redact(str(getattr(exc, "message", None) or exc))[:MAX_ERROR]}
    kind = getattr(exc, "kind", None)
    if isinstance(kind, str) and kind:
        out["errorCode"] = kind
    return out


# ---- Logging -----------------------------------------------------------------------------

# Which process and release wrote a line; set by setup_logging. Pod and namespace come from the collector.
_identity: dict[str, str] = {}

_EXTRA_FIELDS = (
    "event", "route", "method", "status", "durationMs", "outcome", "errorType", "operation",
    "dependency", "investigation_id", "tool", "action", "decision", "connector_type", "request_id", "errorCode",
    "error", "user_id", "job_id", "job_type", "attempt", "plan_id", "environment", "namespace", "node",
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
            **_identity,
        }
        out.update(getattr(record, "_moks_context", None) or _log_context.get())  # what the line is part of; the record's own fields win
        for key in _EXTRA_FIELDS:
            if hasattr(record, key):
                out[key] = getattr(record, key)
        out.setdefault("event", record.getMessage().replace(" ", "_")[:60])
        trace_id, span_id = current_trace_ids()
        if trace_id:
            out["trace_id"], out["span_id"] = trace_id, span_id
        if record.exc_info:
            out.setdefault("errorType", record.exc_info[0].__name__ if record.exc_info[0] else "Exception")
            from .redaction import redact

            if record.exc_info[1] is not None and "error" not in out:
                out["error"] = redact(str(record.exc_info[1]))[:MAX_ERROR]
            out["stack"] = redact(self.formatException(record.exc_info))[-MAX_STACK:]  # the end of a traceback says the most
        return json.dumps(out, default=str)


def _with_context(factory):
    """Snapshots the log context onto each record when it is created, so a handler that formats
    later (a queue, a test capture) still writes the ids of the work that logged it."""
    def make(*args, **kwargs):
        record = factory(*args, **kwargs)
        record._moks_context = _log_context.get()
        return record
    make._moks = True
    return make


def setup_logging(level: str = "INFO", service: str | None = None) -> None:
    from . import __version__
    from .config import get_settings

    commit = (get_settings().build_commit or "")[:12]
    _identity.update({"service": service or "moks", "version": f"{__version__}+{commit}" if commit else __version__})
    if not getattr(logging.getLogRecordFactory(), "_moks", False):
        logging.setLogRecordFactory(_with_context(logging.getLogRecordFactory()))
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

    provider = TracerProvider(sampler=sampler(get_settings().trace_sample_ratio), resource=Resource.create({
        "service.name": service_name, "service.namespace": "moks", "service.version": __version__,
        "service.instance.id": os.environ.get("HOSTNAME", ""), "vcs.ref.head.revision": get_settings().build_commit}))
    # Bounded queue: when the collector is slow or gone, spans are dropped, never waited for.
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(timeout=5), max_queue_size=2048, export_timeout_millis=10000))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument(request_hook=_name_http_span, async_request_hook=_name_http_span_async)
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz", exclude_spans=["send", "receive"])
    _tracing_ready = True
    from . import db

    if db._engine is not None:  # created before tracing was set up
        instrument_engine(db._engine)


def sampler(ratio: float):
    """Children follow their parent. A new trace starts at `ratio`, except an outgoing call with no parent:
    a background HTTP call (search probe, outbox flush) made outside any request or job is a one-span
    trace that says nothing, and on mk4 such spans were three quarters of the worker's traces."""
    from opentelemetry.sdk.trace.sampling import Decision, ParentBased, Sampler, SamplingResult, TraceIdRatioBased
    from opentelemetry.trace import SpanKind

    ratio_sampler = TraceIdRatioBased(max(0.0, min(1.0, ratio)))

    class Roots(Sampler):
        def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None, links=None, trace_state=None):
            if kind == SpanKind.CLIENT:
                return SamplingResult(Decision.DROP)
            return ratio_sampler.should_sample(parent_context, trace_id, name, kind, attributes, links, trace_state)

        def get_description(self) -> str:
            return f"MoksRoots({ratio_sampler.get_description()})"

    return ParentBased(root=Roots())


@contextmanager
def unrecorded() -> Iterator[None]:
    """Keeps the current trace id for log lines but records nothing beneath it. For loops inside a span
    that would otherwise add a child per iteration (an event stream polls twice a second for ten minutes)."""
    from opentelemetry import trace

    with trace.use_span(trace.NonRecordingSpan(trace.get_current_span().get_span_context()), end_on_exit=False):
        yield


def tracing_enabled() -> bool:
    return _tracing_ready


def _name_http_span(span, request) -> None:
    """"GET" alone says nothing in a waterfall: name the span after the host it calls. The host only,
    never the path or query, which can hold PromQL, LogQL or search terms."""
    try:
        method = request.method.decode() if isinstance(request.method, bytes) else str(request.method)
        host = request.url.host.decode() if isinstance(request.url.host, bytes) else request.url.host
        span.update_name(f"{method} {host}")
        span.set_attribute("server.address", host)
    except Exception:  # pragma: no cover - naming is cosmetic
        pass


async def _name_http_span_async(span, request) -> None:
    _name_http_span(span, request)


_SQL_VERB = re.compile(r"^\s*(?:/\*.*?\*/\s*)?([A-Za-z]+)", re.S)


def instrument_engine(engine) -> None:
    """A child span per SQL statement, only inside a span that is already recording, so queries
    show in the request or run that made them and background polling makes no root traces.
    The span names the verb (SELECT, UPDATE...) and never carries the statement or parameters."""
    if not _tracing_ready or getattr(engine.sync_engine, "_moks_traced", False):
        return
    from opentelemetry import trace
    from opentelemetry.trace import SpanKind, Status, StatusCode
    from sqlalchemy import event

    sync = engine.sync_engine

    @event.listens_for(sync, "before_cursor_execute")
    def _start(conn, cursor, statement, parameters, context, executemany):
        if context is None or not trace.get_current_span().is_recording():
            return
        m = _SQL_VERB.match(statement or "")
        verb = m.group(1).upper() if m else "SQL"
        context._moks_span = tracer().start_span(f"db {verb}", kind=SpanKind.CLIENT, attributes={
            "db.system.name": "postgresql", "db.operation.name": verb, "db.batch": bool(executemany)})

    @event.listens_for(sync, "after_cursor_execute")
    def _end(conn, cursor, statement, parameters, context, executemany):
        span = getattr(context, "_moks_span", None)
        if span is not None:
            span.end()
            context._moks_span = None

    @event.listens_for(sync, "handle_error")
    def _failed(exc_ctx):
        context = exc_ctx.execution_context
        span = getattr(context, "_moks_span", None) if context is not None else None
        if span is not None:
            err = exc_ctx.original_exception
            span.set_attribute("error.type", type(err).__name__)
            code = getattr(err, "sqlstate", None) or getattr(getattr(err, "orig", None), "sqlstate", None)
            if code:
                span.set_attribute("db.response.status_code", str(code))
            span.set_status(Status(StatusCode.ERROR, type(err).__name__))
            span.end()
            context._moks_span = None

    sync._moks_traced = True


def tracer():
    from opentelemetry import trace

    return trace.get_tracer("moks")


def mark_failed(span, error: BaseException | str, code: str | None = None) -> None:
    """Marks a span as failed with the error's type and code only. The collector drops exception
    text on purpose (it can hold SQL, addresses or secrets), so the type and code are what a
    trace keeps."""
    from opentelemetry.trace import Status, StatusCode

    kind = error if isinstance(error, str) else type(error).__name__
    code = code or (None if isinstance(error, str) else getattr(error, "kind", None))
    span.set_attribute("error.type", kind)
    if code:
        span.set_attribute("moks.error_code", str(code))
    span.set_status(Status(StatusCode.ERROR, f"{kind}: {code}" if code else kind))


@contextmanager
def span(name: str, context=None, **attributes: Any):
    """A span whose failures are recorded by type and code, never by exception message (OTel's
    default puts str(exc) in the status description, which the collector does not strip).
    Cancellation is not a failure: it is recorded as moks.outcome=cancelled."""
    import asyncio

    with tracer().start_as_current_span(name, context=context, record_exception=False, set_status_on_exception=False,
                                        attributes={k: v for k, v in attributes.items() if v is not None}) as s:
        try:
            yield s
        except asyncio.CancelledError:
            s.set_attribute("moks.outcome", "cancelled")
            raise
        except BaseException as exc:
            mark_failed(s, exc)
            raise


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
