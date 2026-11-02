"""The service's contract, with a stand-in model where the real one would only add minutes."""
import asyncio
import os
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
os.environ["RERANKER_TOKEN"] = "test-token"
os.environ["RERANKER_METRICS_PORT"] = "0"

import app as service  # noqa: E402
from engine import Expired, Gate, Overloaded, Scored  # noqa: E402

AUTH = {"Authorization": "Bearer test-token"}


class Fake:
    id, revision = "org/model", "abc123"

    def __init__(self, scores=None, hold=None, fail=None, load_fails=False):
        self.scores, self.hold, self.fail, self.load_fails = scores, hold, fail, load_fails
        self.calls = 0

    def load(self):
        if self.load_fails:
            raise OSError("no weights")

    def warm_up(self):
        pass

    def score(self, query, candidates, deadline):
        self.calls += 1
        if self.hold:
            self.hold.wait(5)
        if self.fail:
            raise self.fail
        return Scored(self.scores if self.scores is not None else [float(len(t)) for _, t in candidates], [c for c, t in candidates if len(t) > 100], 3, 512)


def body(**over):
    return {"request_id": "r1", "query": "why", "candidates": [{"id": "a", "text": "x"}, {"id": "b", "text": "xxx"}], "limit": 2, **over}


@pytest.fixture
def run():
    def start(model=None, gate=None):
        return TestClient(service.create_app(model or Fake(), gate or Gate(1, 1)), raise_server_exceptions=False)
    return start


def test_ranks_highest_first_and_says_what_scored_it(run):
    with run() as c:
        r = c.post("/rerank", json=body(), headers=AUTH).json()
    assert [x["id"] for x in r["results"]] == ["b", "a"] and r["model"] == {"id": "org/model", "revision": "abc123"} and r["duration_ms"] >= 0


def test_the_limit_applies(run):
    with run() as c:
        assert len(c.post("/rerank", json=body(limit=1), headers=AUTH).json()["results"]) == 1


def test_truncation_is_reported(run):
    with run() as c:
        t = c.post("/rerank", json=body(candidates=[{"id": "a", "text": "y" * 200}]), headers=AUTH).json()["truncation"]
    assert t["truncated_ids"] == ["a"] and t["max_length_tokens"] == 512


def test_no_token_no_inference(run):
    m = Fake()
    with run(m) as c:
        assert c.post("/rerank", json=body()).status_code == 401
        assert c.post("/rerank", json=body(), headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert m.calls == 0


def test_the_service_refuses_to_start_without_a_token(monkeypatch):
    monkeypatch.delenv("RERANKER_TOKEN")
    with pytest.raises(RuntimeError):
        service.create_app(Fake())


@pytest.mark.parametrize("bad", [
    {"candidates": []}, {"candidates": [{"id": f"c{i}", "text": "x"} for i in range(51)]}, {"query": "q" * 2001}, {"query": ""},
    {"candidates": [{"id": "a", "text": "x" * 8001}]}, {"candidates": [{"id": "a", "text": "x"}, {"id": "a", "text": "y"}]},
    {"limit": 0}, {"limit": "2"}, {"model": "evil/model"}, {"model_path": "/etc/passwd"}, {"trust_remote_code": True},
    {"candidates": [{"id": "../a", "text": "x"}]}, {"candidates": [{"id": "a", "text": "x", "url": "http://x"}]},
])
def test_invalid_requests_are_refused_and_not_echoed(run, bad):
    m = Fake()
    with run(m) as c:
        r = c.post("/rerank", json=body(**bad), headers=AUTH)
    assert r.status_code == 422 and m.calls == 0
    assert "x" * 50 not in r.text and "q" * 50 not in r.text


def test_a_deployment_may_lower_a_limit_but_never_raise_it(run, monkeypatch):
    monkeypatch.setenv("RERANKER_MAX_CANDIDATES", "1")
    with run() as c:
        assert c.post("/rerank", json=body(), headers=AUTH).status_code == 422
    monkeypatch.setenv("RERANKER_MAX_CANDIDATES", "9999")
    with run() as c:
        assert c.post("/rerank", json=body(candidates=[{"id": f"c{i}", "text": "x"} for i in range(51)]), headers=AUTH).status_code == 422


@pytest.mark.parametrize("scores", [[1.0], [float("nan"), 1.0], [float("inf"), 1.0]])
def test_invalid_model_output_is_an_error_not_a_ranking(run, scores):
    with run(Fake(scores=scores)) as c:
        assert c.post("/rerank", json=body(), headers=AUTH).status_code == 500


def test_a_model_failure_is_a_clean_error(run):
    with run(Fake(fail=RuntimeError("secret passage text"))) as c:
        r = c.post("/rerank", json=body(), headers=AUTH)
    assert r.status_code == 500 and "secret" not in r.text


def test_not_ready_until_the_model_loads(run):
    with run(Fake(load_fails=True)) as c:
        assert c.get("/readyz").status_code == 503 and c.get("/healthz").status_code == 200
        assert c.post("/rerank", json=body(), headers=AUTH).status_code == 503


def test_ready_reports_the_pinned_model(run):
    with run() as c:
        assert c.get("/readyz").json()["model"]["revision"] == "abc123"


def test_there_is_no_generated_api_surface(run):
    with run() as c:
        assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404


def test_queries_and_passages_are_never_logged(run, caplog):
    caplog.set_level("INFO")
    with run() as c:
        c.post("/rerank", json=body(query="supersecretquery", candidates=[{"id": "a", "text": "supersecretpassage"}]), headers=AUTH)
    assert "supersecret" not in caplog.text


# ---- capacity ----------------------------------------------------------------------------------

def test_overload_is_a_clear_refusal():
    release = threading.Event()
    gate = Gate(1, 0)
    gate.submit(lambda w: release.wait(5))
    with pytest.raises(Overloaded):
        gate.submit(lambda w: None)
    release.set()


async def test_a_cancelled_caller_does_not_free_a_running_slot():
    release = threading.Event()
    gate = Gate(1, 0)
    future = gate.submit(lambda w: release.wait(5))
    waiter = asyncio.ensure_future(asyncio.shield(asyncio.wrap_future(future)))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert gate.held == 1  # the caller left, the CPU is still busy
    with pytest.raises(Overloaded):
        gate.submit(lambda w: None)
    release.set()
    await asyncio.wrap_future(future)
    for _ in range(100):
        if gate.held == 0:
            break
        await asyncio.sleep(0.01)
    assert gate.held == 0
    gate.submit(lambda w: None).result(5)


def test_a_timed_out_request_keeps_its_slot_until_the_work_ends():
    release = threading.Event()
    gate = Gate(1, 0)
    m = Fake(hold=release)

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post("/rerank", json=body(timeout_ms=30), headers=AUTH)

    app = service.create_app(m, gate)
    with TestClient(app):
        r = asyncio.run(go())
        assert r.status_code == 504 and gate.held == 1
        release.set()
        for _ in range(200):
            if gate.held == 0:
                break
            time.sleep(0.01)
    assert gate.held == 0


def test_work_past_its_deadline_never_starts():
    release = threading.Event()
    gate = Gate(1, 1)
    m = Fake()
    gate.submit(lambda w: release.wait(5))
    deadline = time.monotonic() - 1

    def late(waited):
        if time.monotonic() > deadline:
            raise Expired()
        return m.score("q", [], 0)
    f = gate.submit(late)
    release.set()
    with pytest.raises(Expired):
        f.result(5)
    assert m.calls == 0


def test_shutdown_refuses_new_work_and_finishes_what_it_accepted():
    done = []
    gate = Gate(1, 1)
    gate.submit(lambda w: (time.sleep(0.05), done.append(1)))
    gate.close()
    assert done == [1]
    with pytest.raises(Overloaded):
        gate.submit(lambda w: None)


def test_a_deadline_stops_between_batches(tmp_path):
    from engine import Model

    class Encoder:
        def __init__(self):
            self.batches = 0

        def predict(self, pairs, **kw):
            self.batches += 1
            time.sleep(0.03)
            return [0.0] * len(pairs)

    class Tok:
        def __call__(self, text, **kw):
            return {"input_ids": [0]}

        def num_special_tokens_to_add(self, pair):
            return 3

    m = Model(str(tmp_path), 1, 1)
    m._encoder, m._tokenizer, m._identity = Encoder(), Tok(), None
    with pytest.raises(Expired):
        m.score("q", [(str(i), "t") for i in range(50)], time.monotonic() + 0.05)
    assert m._encoder.batches < 50


def test_a_missing_lock_file_refuses_to_load(tmp_path):
    from engine import Model, ModelError

    with pytest.raises(ModelError):
        Model(str(tmp_path), 1, 1).load()


def test_fetching_detects_a_tampered_file(tmp_path):
    import fetch_model

    (tmp_path / "f").write_bytes(b"weights")
    with pytest.raises(SystemExit):
        fetch_model.verify({"files": {"f": "0" * 64}}, tmp_path)
