"""One error shape for API failures, log context that follows a unit of work, and spans that
say what failed without carrying exception text."""
import json
import logging

import pytest
from fastapi import APIRouter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from moks import observability
from moks.connectors.base import ConnectorError
from moks.observability import JsonFormatter, bind, bound, error_fields, log_context

from .conftest import login


def line(msg="x", exc=None, **extra):
    r = logging.LogRecord("moks.test", logging.ERROR, __file__, 1, msg, None, exc)
    r.__dict__.update(extra)
    return json.loads(JsonFormatter().format(r))


# ---- Error envelope ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def broken_routes(app):
    """Routes that fail in each way a real route can, mounted once for the module."""
    router = APIRouter(prefix="/moks/api/_test")

    @router.get("/boom")
    async def boom():
        raise RuntimeError("password=hunter2 leaked into an exception")

    @router.get("/connector")
    async def connector():
        raise ConnectorError("timeout", "Prometheus did not answer within 10s")

    @router.get("/limit")
    async def limit(n: int):
        return {"n": n}

    app.router.routes[:0] = router.routes  # ahead of the API's catch-all 404
    return app


async def test_not_found_has_code_message_and_reference(client):
    r = await client.get("/moks/api/no-such-thing")
    assert r.status_code == 404
    body = r.json()
    assert body["detail"] == "Not found"  # unchanged for existing clients
    assert body["error"]["code"] == "not_found" and body["error"]["status"] == 404
    assert body["error"]["request_id"] == r.headers["x-request-id"]


async def test_a_plain_caller_request_id_is_kept_and_a_hostile_one_replaced(client):
    r = await client.get("/moks/api/no-such-thing", headers={"X-Request-ID": "client-req-0001"})
    assert r.headers["x-request-id"] == "client-req-0001" and r.json()["error"]["request_id"] == "client-req-0001"
    r = await client.get("/moks/api/no-such-thing", headers={"X-Request-ID": 'x"}\n{"level":"error'})
    assert r.headers["x-request-id"] != 'x"}\n{"level":"error' and len(r.headers["x-request-id"]) == 32


async def test_unhandled_error_is_a_generic_500_with_the_stack_in_the_log(client, broken_routes, caplog):
    with caplog.at_level(logging.ERROR, logger="moks.api"):
        r = await client.get("/moks/api/_test/boom")
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "internal" and "hunter2" not in r.text
    rid = body["error"]["request_id"]
    unhandled = [rec for rec in caplog.records if getattr(rec, "event", None) == "http_unhandled"]
    assert unhandled and unhandled[0].exc_info
    out = json.loads(JsonFormatter().format(unhandled[0]))
    assert out["request_id"] == rid and out["route"] == "/moks/api/_test/boom"
    assert "hunter2" not in out["stack"] and "hunter2" not in out["error"]  # redacted


async def test_connector_errors_map_to_upstream_statuses(client, broken_routes):
    r = await client.get("/moks/api/_test/connector")
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "connector_timeout"
    assert r.json()["detail"] == "Prometheus did not answer within 10s"


async def test_validation_errors_are_readable_and_listed(client, broken_routes):
    r = await client.get("/moks/api/_test/limit?n=many")
    assert r.status_code == 422
    body = r.json()
    assert isinstance(body["detail"], str) and body["detail"].startswith("Invalid request — n:")
    assert body["error"]["fields"][0]["field"] == "query.n"


async def test_signed_in_requests_log_the_user(client, caplog):
    await login(client, "ctx-user@example.com")
    with caplog.at_level(logging.INFO, logger="moks.api"):
        await client.get("/moks/api/environments")
    lines = [rec for rec in caplog.records if getattr(rec, "event", None) == "http_request" and rec.route == "/moks/api/environments"]
    assert lines and getattr(lines[-1], "user_id", None)


# ---- Log context -------------------------------------------------------------------------

def test_bound_fields_reach_every_line_and_go_away_after():
    with bound(job_id="j1", job_type="knowledge_sync", skipped=None):
        bind(environment="mk4")
        out = line("inside")
        assert out["job_id"] == "j1" and out["environment"] == "mk4" and "skipped" not in out
    assert "job_id" not in line("after") and "environment" not in log_context()


def test_a_records_own_field_wins_over_the_context():
    with bound(operation="outer"):
        assert line("x", operation="inner")["operation"] == "inner"


def test_error_fields_carry_the_kind_and_a_redacted_message():
    f = error_fields(ConnectorError("auth", "token=abcdefghijklmnopqrstuvwxyz0123456789 rejected"))
    assert f["errorType"] == "ConnectorError" and f["errorCode"] == "auth"
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in f["error"]
    assert "errorCode" not in error_fields(ValueError("plain"))


# ---- Spans -------------------------------------------------------------------------------

@pytest.fixture
def exported(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(observability, "tracer", lambda: provider.get_tracer("moks"))
    return exporter


def test_a_failed_span_keeps_the_type_and_code_but_not_the_message(exported):
    with pytest.raises(ConnectorError):
        with observability.span("tool prom_query", **{"moks.tool": "prom_query", "moks.namespace": None}):
            raise ConnectorError("timeout", "query rate(secret_metric[5m]) timed out")
    (s,) = exported.get_finished_spans()
    assert s.status.status_code == StatusCode.ERROR
    assert s.attributes["error.type"] == "ConnectorError" and s.attributes["moks.error_code"] == "timeout"
    assert "moks.namespace" not in s.attributes
    assert "secret_metric" not in (s.status.description or "") and not s.events


async def test_cancellation_is_not_a_failure(exported):
    import asyncio

    with pytest.raises(asyncio.CancelledError):
        with observability.span("investigation"):
            raise asyncio.CancelledError()
    (s,) = exported.get_finished_spans()
    assert s.status.status_code != StatusCode.ERROR and s.attributes["moks.outcome"] == "cancelled"


async def test_sql_runs_as_child_spans_only_inside_a_recording_span(exported, monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from moks.config import get_settings

    monkeypatch.setattr(observability, "_tracing_ready", True)
    engine = create_async_engine(get_settings().database_url)
    try:
        observability.instrument_engine(engine)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))  # no parent: no span, so polling makes no root traces
            with observability.span("job knowledge_sync"):
                await conn.execute(text("/* comment */ select 2"))
                with pytest.raises(Exception):
                    await conn.execute(text("SELECT * FROM no_such_table_here"))
    finally:
        await engine.dispose()
    names = [s.name for s in exported.get_finished_spans()]
    assert names.count("db SELECT") == 2 and "job knowledge_sync" in names
    failed = [s for s in exported.get_finished_spans() if s.name == "db SELECT" and s.status.status_code == StatusCode.ERROR]
    assert failed and failed[0].attributes["db.response.status_code"] == "42P01"
    parent = next(s for s in exported.get_finished_spans() if s.name == "job knowledge_sync")
    assert all(s.parent.span_id == parent.context.span_id for s in exported.get_finished_spans() if s.name == "db SELECT")


def test_http_spans_are_named_after_the_host_only():
    import httpx

    class Span:
        def update_name(self, n): self.name = n
        def set_attribute(self, k, v): setattr(self, k.replace(".", "_"), v)

    s = Span()
    req = httpx.Request("GET", "http://prometheus.monitoring:9090/prometheus/api/v1/query?query=up")
    observability._name_http_span(s, type("Info", (), {"method": b"GET", "url": req.url})())
    assert s.name == "GET prometheus.monitoring" and "query" not in s.name
