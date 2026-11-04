"""Where MOKS's own telemetry starts and stops: streams, background calls, embeddings, knowledge search
and model fallback. Each test uses an in-memory span exporter; nothing is sent anywhere."""
import asyncio
import uuid

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from moks import observability
from moks.db import engine as db_engine
from tests.conftest import login, make_env, make_user


@pytest.fixture
def exported(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(observability, "tracer", lambda: provider.get_tracer("moks"))
    return exporter, provider.get_tracer("test")


async def _running(client):
    name = f"tel-{uuid.uuid4().hex[:6]}"
    await make_env(name, connectors=[{"type": "kubernetes", "name": "k8s", "config": {"mode": "in_cluster"}}])
    email = f"tel-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(email, [f"investigator:{name}:*"])
    h = await login(client, email)
    r = await client.post("/moks/api/investigations", json={"question": "what broke?", "environment": name, "namespace": "shop"}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---- Event streams -----------------------------------------------------------------------

async def test_an_open_event_stream_makes_no_span_per_poll_and_says_how_it_ended(client, monkeypatch, exported):
    """The stream polls the database every half second for up to ten minutes. Inside the request's span each
    poll used to become SQL child spans, thousands per trace, which crowded the exporter queue."""
    from moks.api import investigations

    exporter, test_tracer = exported
    monkeypatch.setattr(observability, "_tracing_ready", True)
    db_engine().sync_engine._moks_traced = False  # instrument the shared engine for this test's tracer
    observability.instrument_engine(db_engine())
    monkeypatch.setattr(investigations, "STREAM_SECONDS", 1.2)
    iid = await _running(client)
    exporter.clear()
    with test_tracer.start_as_current_span("GET /moks/api/investigations/{inv_id}/events", kind=SpanKind.SERVER):
        r = await client.get(f"/moks/api/investigations/{iid}/events")
    assert b"event: timeout" in r.content
    spans = exporter.get_finished_spans()
    polls = [s for s in spans if s.name.startswith("db ")]
    assert len(polls) <= 6, f"{len(polls)} SQL spans from one stream"  # the handler's own lookups, not one per poll
    server = next(s for s in spans if s.name.endswith("/events"))
    assert server.attributes["moks.stream.outcome"] == "timeout"
    assert server.attributes["moks.stream.events"] >= 1


async def test_stream_metrics_count_open_and_finished_streams(client, monkeypatch):
    from moks.api import investigations

    monkeypatch.setattr(investigations, "STREAM_SECONDS", 0.3)
    iid = await _running(client)
    before = observability.SSE_STREAMS.labels("timeout")._value.get()
    await client.get(f"/moks/api/investigations/{iid}/events")
    assert observability.SSE_STREAMS.labels("timeout")._value.get() == before + 1
    assert observability.SSE_STREAMS_OPEN._value.get() == 0


# ---- Background HTTP calls ---------------------------------------------------------------

def test_a_client_call_with_no_parent_is_not_sampled():
    """150 of 200 recent moks-worker traces on mk4 were a single unnamed POST: background calls with no
    request or job around them. They carried no information and pushed real traces out of Jaeger."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=observability.sampler(1.0))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    t = provider.get_tracer("t")
    with t.start_as_current_span("POST", kind=SpanKind.CLIENT):
        pass
    with t.start_as_current_span("job knowledge_sync"):
        with t.start_as_current_span("POST moks-search", kind=SpanKind.CLIENT):
            pass
    assert [s.name for s in exporter.get_finished_spans()] == ["POST moks-search", "job knowledge_sync"]


def test_the_sample_ratio_applies_to_roots_and_children_follow():
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=observability.sampler(0.0))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    t = provider.get_tracer("t")
    with t.start_as_current_span("investigation"):
        with t.start_as_current_span("tool k8s_list_pods"):
            pass
    assert exporter.get_finished_spans() == ()


# ---- Embeddings --------------------------------------------------------------------------

async def test_an_embedding_call_is_a_span_and_a_metric(exported, monkeypatch):
    from moks.llm import budget
    from moks.llm.providers import LLMError

    exporter, _ = exported

    class Cfg:
        provider, model, id = "gemini", "gemini-embedding-001", uuid.uuid4()
        input_price_per_mtok = output_price_per_mtok = 0

    class Ok:
        cfg = Cfg()
        async def embed(self, texts): return [[0.1, 0.2]] * len(texts)

    class Down(Ok):
        async def embed(self, texts): raise LLMError("unavailable", "cannot reach gemini (ConnectError) key=abc123")

    async def reserve(*a, **k): return uuid.uuid4()
    async def settle(*a, **k): return None
    async def close(*a, **k): return None
    monkeypatch.setattr(budget, "reserve", reserve)
    monkeypatch.setattr(budget, "settle", settle)
    monkeypatch.setattr(budget, "close", close)
    before = observability.EMBED_REQUESTS.labels("gemini", "unavailable")._value.get()
    await budget.embed(Ok(), Cfg(), ["a", "b"], purpose="search")
    with pytest.raises(LLMError):
        await budget.embed(Down(), Cfg(), ["a"], purpose="indexing")
    ok, failed = [s for s in exporter.get_finished_spans() if s.name == "llm.embed"]
    assert ok.attributes["moks.embed.texts"] == 2 and ok.attributes["moks.purpose"] == "search"
    assert failed.status.status_code == StatusCode.ERROR and failed.attributes["moks.error_code"] == "unavailable"
    assert "abc123" not in (failed.status.description or "")
    assert observability.EMBED_REQUESTS.labels("gemini", "unavailable")._value.get() == before + 1


# ---- Model fallback and retries ----------------------------------------------------------

async def test_the_model_that_answered_and_retries_are_on_the_span(exported, monkeypatch):
    from moks.llm import providers
    from moks.llm.providers import LLMResponse

    exporter, _ = exported

    class Cfg:
        provider, model = "anthropic", "claude-opus-5-5"
        input_price_per_mtok = output_price_per_mtok = 0

    class Fallen:
        cfg = Cfg()
        async def complete(self, system, messages, *, json_mode, max_tokens):
            providers.note_retry()
            return LLMResponse("ok", 10, 5, "anthropic", "claude-opus-5-5", response_model="claude-sonnet-5-5", fallback=True)

    await providers.complete_metered(Fallen(), "s", [], json_mode=False, max_tokens=10)
    (s,) = [s for s in exporter.get_finished_spans() if s.name == "llm.complete"]
    assert s.attributes["gen_ai.response.model"] == "claude-sonnet-5-5"
    assert s.attributes["moks.llm.fallback"] is True
    assert s.attributes["moks.llm.retries"] == 1


async def test_openai_style_retries_are_counted(monkeypatch):
    from moks.llm import providers

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"ok": True})

    class Cfg:
        provider, model, timeout_seconds = "openai", "m", 5

    p = providers.Provider(Cfg(), transport=httpx.MockTransport(handler))
    before = observability.LLM_RETRIES.labels("openai")._value.get()
    with providers.counting_retries() as retries:
        await p._post("http://llm.test/v1/chat", {}, {})
    assert retries() == 1 and observability.LLM_RETRIES.labels("openai")._value.get() == before + 1


# ---- Knowledge search --------------------------------------------------------------------

async def test_a_knowledge_search_is_one_span_with_counts_but_not_the_query(exported, monkeypatch):
    from moks.db import session_scope
    from moks.rag import service
    from moks.rbac import Binding, Principal

    exporter, _ = exported
    principal = Principal(user_id=str(uuid.uuid4()), email="t@example.com", bindings=[Binding("admin", "*", "*")])
    async with session_scope() as db:
        out = await service.search(db, principal, "checkout password=hunter2 timeout", k=3, retrieval="keyword")
    (s,) = [s for s in exporter.get_finished_spans() if s.name == "rag.search"]
    assert s.attributes["moks.rag.mode"] == out["mode"]
    assert s.attributes["moks.rag.results"] == len(out["results"])
    assert s.attributes["moks.rag.requested_mode"] == "keyword"
    assert not any("hunter2" in str(v) for v in s.attributes.values())


# ---- Logs say which process and release wrote them ---------------------------------------

def test_lines_name_the_service_and_release(monkeypatch):
    import json
    import logging

    monkeypatch.setattr(observability, "_identity", {"service": "moks-worker", "version": "0.1.0+abc1234"})
    r = logging.LogRecord("moks.test", logging.INFO, __file__, 1, "hello", None, None)
    out = json.loads(observability.JsonFormatter().format(r))
    assert out["service"] == "moks-worker" and out["version"] == "0.1.0+abc1234"


# ---- The investigation gives its trace id back ---------------------------------------------

async def test_an_investigation_reports_its_trace_id(client, exported):
    _, test_tracer = exported
    with test_tracer.start_as_current_span("POST /moks/api/investigations") as parent:
        iid = await _running(client)
    r = await client.get(f"/moks/api/investigations/{iid}")
    assert r.json()["trace_id"] == format(parent.get_span_context().trace_id, "032x")


async def test_telemetry_that_cannot_be_exported_does_not_fail_a_request(client, monkeypatch):
    """An exporter that raises on every batch: the request still answers."""
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

    class Broken(SpanExporter):
        def export(self, spans):
            raise ConnectionError("collector unreachable")
        def shutdown(self): pass

    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(Broken(), schedule_delay_millis=10))
    monkeypatch.setattr(observability, "tracer", lambda: provider.get_tracer("moks"))
    with provider.get_tracer("t").start_as_current_span("GET"):
        r = await client.get("/moks/api/health")
    await asyncio.sleep(0.05)
    provider.shutdown()
    assert r.status_code == 200


# ---- A model answer that could not be used says why --------------------------------------

def test_an_unusable_model_answer_is_counted_with_a_fixed_reason(exported, caplog):
    import logging

    _, t = exported
    before = observability.LLM_ANSWERS_UNUSED.labels("report", "schema_invalid")._value.get()
    with caplog.at_level(logging.WARNING, logger="moks.llm"), t.start_as_current_span("node report") as s:
        reason = observability.llm_answer_unused("report", ValueError("report schema invalid: Field required"))
    assert reason == "schema_invalid" and s.attributes["moks.llm.answer_unused"] == "schema_invalid"
    assert observability.LLM_ANSWERS_UNUSED.labels("report", "schema_invalid")._value.get() == before + 1
    line = next(r for r in caplog.records if getattr(r, "event", "") == "llm_answer_unused")
    assert line.errorCode == "schema_invalid" and "Field required" not in line.getMessage()
