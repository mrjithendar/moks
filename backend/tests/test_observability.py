"""JSON logs in the shape the log pipeline keeps, and traces that hold together."""
import json
import logging
import re
import uuid
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from moks import jobs, observability
from moks.connectors import registry
from moks.connectors.base import ConnectorError, HttpLimits, BoundedHttp, resolve_credential
from moks.db import session_scope
from moks.models import Job
from moks.observability import JsonFormatter


def record(msg="something", exc=None, **extra):
    r = logging.LogRecord("moks.test", logging.WARNING, __file__, 1, msg, None, exc)
    r.__dict__.update(extra)
    return json.loads(JsonFormatter().format(r))


def test_lines_carry_the_fields_the_pipeline_keeps():
    out = record(event="job_finished", operation="knowledge_sync", outcome="success", durationMs=12, dependency="loki", errorCode="timeout", route="/moks/api/x", method="GET", status=200)
    keep = Path(__file__).parents[3] / "tools/observability/loki/config.alloy"
    if keep.exists():
        fields = set(re.findall(r'raw_\w+ = "(\w+)"', keep.read_text()))
        assert fields - set(out) <= {"errorType"}  # only set when something failed
    assert out["level"] == "warning" and out["logger_name"] == "moks.test"


def test_the_collector_is_told_to_read_moks():
    config = Path(__file__).parents[3] / "tools/observability/loki/config.alloy"
    if config.exists():
        assert '"moks"' in re.search(r"names = \[(.*?)\]", config.read_text()).group(1)


def test_stack_traces_are_json_and_redacted():
    try:
        raise ValueError("password=hunter2 failed")
    except ValueError:
        import sys

        out = record(exc=sys.exc_info())
    assert out["errorType"] == "ValueError" and "test_stack_traces" in out["stack"] and "hunter2" not in out["stack"]


def test_setup_routes_warnings_and_crashes_to_json(capsys):
    root, hook = logging.getLogger(), __import__("sys").excepthook
    before = root.handlers[:], root.level
    try:
        observability.setup_logging("INFO")
        logging.getLogger("py.warnings").warning("old thing")
        __import__("sys").excepthook(RuntimeError, RuntimeError("boom"), None)
        lines = [json.loads(x) for x in capsys.readouterr().out.strip().splitlines()]
    finally:
        logging.captureWarnings(False)
        root.handlers[:], _ = before[0], root.setLevel(before[1])
        __import__("sys").excepthook = hook
    assert any("old thing" in x["message"] for x in lines) and any(x.get("event") == "process_crashed" for x in lines)


async def test_a_failed_connector_call_is_logged_without_its_url_query(caplog):
    import httpx

    http = BoundedHttp("loki", "http://x", limits=HttpLimits(retries=0), transport=httpx.MockTransport(lambda r: httpx.Response(500, text="no")))
    with caplog.at_level(logging.WARNING, "moks.connectors"), pytest.raises(ConnectorError):
        await http.get_json("query", "/q", params={"q": "secret-search"})
    rec = next(r for r in caplog.records if getattr(r, "event", "") == "connector_request_failed")
    assert rec.dependency == "loki" and rec.outcome == "failure" and "secret-search" not in rec.getMessage()


async def test_a_redirect_is_explained():
    import httpx

    http = BoundedHttp("loki", "http://x", limits=HttpLimits(retries=0), transport=httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": "https://y/"})))
    with pytest.raises(ConnectorError, match="https://y/"):
        await http.get_json("query", "/q")


def test_credentials_lose_a_trailing_newline(monkeypatch):
    monkeypatch.setenv("MOKS_CRED_X", "token\n")
    assert resolve_credential({"type": "env", "name": "MOKS_CRED_X"}) == "token"


async def test_a_changed_connector_keeps_the_old_client_for_calls_in_flight(monkeypatch):
    from moks.models import Connector

    closed = []

    class C:
        def __init__(self, *a):
            pass

        async def aclose(self):
            closed.append(self)
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "loki", C)
    monkeypatch.setattr(registry, "CLOSE_DELAY", 0)
    row = Connector(id=uuid.uuid4(), type="loki", config={"a": 1}, credential_ref=None)
    first = await registry.instance(row)
    row.config = {"a": 2}
    second = await registry.instance(row)
    assert first is not second and not closed
    import asyncio

    await asyncio.sleep(0.01)
    assert closed == [first]


async def test_bad_new_settings_leave_the_running_client(monkeypatch):
    from moks.models import Connector

    class C:
        def __init__(self, config, ref=None):
            if config.get("bad"):
                raise ConnectorError("config", "bad")

        async def aclose(self):
            pass
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "loki", C)
    row = Connector(id=uuid.uuid4(), type="loki", config={}, credential_ref=None)
    first = await registry.instance(row)
    row.config = {"bad": True}
    with pytest.raises(ConnectorError):
        await registry.instance(row)
    assert registry._cache[row.id][1] is first


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("t")
    yield exporter, tracer


async def test_queued_work_continues_the_trace_that_queued_it(spans, monkeypatch):
    exporter, tracer = spans
    monkeypatch.setattr(jobs, "tracer", lambda: tracer)
    with tracer.start_as_current_span("request") as parent:
        jid = await jobs.enqueue("eval_run", {"run_id": str(uuid.uuid4())})
    async with session_scope() as db:
        job = await db.get(Job, jid)
        assert job.payload["_traceparent"].split("-")[1] == format(parent.get_span_context().trace_id, "032x")
        job.status, job.worker_id = "running", "t"
    await jobs.run_one(jid)
    span = next(s for s in exporter.get_finished_spans() if s.name == "job eval_run")
    assert span.context.trace_id == parent.get_span_context().trace_id and span.parent.span_id == parent.get_span_context().span_id
    assert trace is not None


def test_traceparent_is_empty_without_a_span():
    assert observability.traceparent() is None and observability.continue_trace(None) is None
