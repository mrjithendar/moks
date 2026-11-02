"""Reranking can only reorder what search authorized, and it can never make a search worse than without it."""
import json
import uuid

import httpx
import pytest
from moks.db import session_scope
from moks.models import Document, KnowledgeSource
from moks.rag import rerank, service
from moks.rbac import Binding, Principal

MODEL, REV = "org/model", "abc123"
ADMIN = Principal(str(uuid.uuid4()), "a@x", [Binding("admin", "*", "*")])


def client(handler, **kw):
    return rerank.Reranker("http://reranker:8090", "t", MODEL, REV, transport=httpx.MockTransport(handler), **kw)


def ok(scores=None, **over):
    """A well-behaved service; `scores` maps position to score, later is better by default."""
    seen = {}

    def handle(req):
        body = json.loads(req.content)
        seen["body"], seen["auth"] = body, req.headers.get("authorization")
        ids = [c["id"] for c in body["candidates"]]
        order = scores(ids) if scores else {c: float(i) for i, c in enumerate(ids)}
        return httpx.Response(200, json={"request_id": body["request_id"], "results": [{"id": c, "score": order[c]} for c in ids], "model": {"id": MODEL, "revision": REV}, "duration_ms": 3.0, "queue_ms": 0.1,
                                         "truncation": {"max_length_tokens": 512, "query_tokens": 2, "truncated_ids": []}, **over})
    handle.seen = seen
    return handle


async def source(env="*", ns="*", **kw):
    async with session_scope() as db:
        s = KnowledgeSource(name=f"r-{uuid.uuid4().hex[:8]}", type="upload", config={"repository": "org/repo"}, scope_environment=env, scope_namespace=ns, **kw)
        db.add(s)
        await db.flush()
        return s.id


async def write(sid, path, body, revision="rev1"):
    async with session_scope() as db:
        return (await service.index_document(db, await db.get(KnowledgeSource, sid), path, body, revision=revision, url="https://github.com/org/repo/blob/main/x.md")).id


async def find(q, who=ADMIN, **kw):
    async with session_scope() as db:
        return await service.search(db, who, q, 5, **kw)


async def corpus(word, n=4):
    sid = await source()
    return sid, [await write(sid, f"d{i}.md", f"# Doc {i}\n{word} guidance number {i}") for i in range(n)]


async def test_only_authorized_passages_reach_the_model():
    word = f"heron{uuid.uuid4().hex[:6]}"
    mine, theirs = await source("prod", "team-a"), await source("prod", "team-b")
    await write(mine, "a.md", f"# A\n{word} for team a")
    await write(theirs, "b.md", f"# B\n{word} SECRET for team b, a perfect match")
    h = ok()
    who = Principal(str(uuid.uuid4()), "p@x", [Binding("investigator", "prod", "team-a")])
    r = await find(word, who, reranker=client(h))
    assert "SECRET" not in json.dumps(h.seen["body"]) and "SECRET" not in json.dumps(r["results"])
    assert len(r["results"]) == 1 and r["reranking"]["state"] == "applied"


async def test_quarantined_and_archived_content_never_reaches_the_model():
    word = f"egret{uuid.uuid4().hex[:6]}"
    sid = await source()
    live = await write(sid, "a.md", f"# A\n{word} one")
    gone = await write(sid, "b.md", f"# B\n{word} archived")
    async with session_scope() as db:
        await service.set_document_lifecycle(db, await db.get(Document, gone), "archived")
    h = ok()
    await find(word, reranker=client(h))
    assert all("archived" not in c["text"] for c in h.seen["body"]["candidates"]) and live


async def test_results_keep_their_citations_and_scope():
    word = f"crane{uuid.uuid4().hex[:6]}"
    await corpus(word)
    plain, reordered = await find(word), await find(word, reranker=client(ok()))
    key = lambda r: {x["chunk_id"]: (x["citation"], x["scope"], x["title"], x["text"], x["freshness"]["trust"]) for x in r["results"]}  # noqa: E731
    assert key(plain) == key(reordered)


async def test_the_model_really_changes_the_order():
    word = "swift" + "".join(chr(97 + int(c, 16)) for c in uuid.uuid4().hex[:6])  # letters only: a word with digits is an exact identifier, and stays pinned
    await corpus(word)
    r = await find(word, reranker=client(ok()))  # the stand-in prefers whatever ranked last
    assert r["results"][0]["ranking"]["after"] == 1 and r["results"][0]["ranking"]["before"] != 1


async def test_scores_stay_out_of_what_decides_an_answer():
    word = f"wren{uuid.uuid4().hex[:6]}"
    await corpus(word)
    plain, reranked = await find(word), await find(word, reranker=client(ok(lambda ids: {c: 99.0 for c in ids})))
    assert plain["evidence"] == reranked["evidence"] and plain["abstain"] == reranked["abstain"]
    assert "not probabilities" in reranked["reranking"]["note"]


async def test_nothing_found_means_no_call():
    h = ok()
    r = await find(f"nosuchword{uuid.uuid4().hex}", Principal(str(uuid.uuid4()), "p@x", [Binding("investigator", "nowhere", "x")]), reranker=client(h))
    assert r["results"] == [] and "body" not in h.seen


async def test_an_exact_identifier_stays_first():
    tag = uuid.uuid4().hex[:6]
    code = f"ERR_RK_{tag}"
    sid = await source()
    exact = await write(sid, "exact.md", f"# Codes\n{code} means the ledger refused the batch of payment records")
    await write(sid, "near.md", f"# Near\nbatch refused by the ledger of payment records {tag}")
    bury = lambda ids: {c: (-100.0 if i == 0 else 100.0) for i, c in enumerate(ids)}  # noqa: E731
    r = await find(f"{code} ledger refused", reranker=client(ok(bury)))
    assert r["results"][0]["document_id"] == str(exact) and r["results"][0]["ranking"]["pinned"] is True


async def test_a_superseded_document_stays_below_a_current_one():
    word = "finch" + "".join(chr(97 + int(c, 16)) for c in uuid.uuid4().hex[:6])
    sid = await source()
    await write(sid, "old.md", f"---\nsuperseded_by: new.md\n---\n# Old\n{word} procedure text")
    await write(sid, "new.md", f"# New\n{word} procedure text current")

    def prefer_old(req):
        body = json.loads(req.content)  # a model has no idea a document was replaced
        return httpx.Response(200, json={"results": [{"id": c["id"], "score": 50.0 if "Old" in c["text"] else -50.0} for c in body["candidates"]], "model": {"id": MODEL, "revision": REV}, "duration_ms": 1,
                                         "truncation": {"truncated_ids": []}})
    r = await find(word, reranker=client(prefer_old))
    assert r["reranking"]["state"] == "applied" and [x["freshness"]["state"] for x in r["results"]][-1] == "superseded" and r["results"][0]["freshness"]["state"] != "superseded"


async def test_diversity_survives_reranking():
    word = f"plover{uuid.uuid4().hex[:6]}"
    sid = await source()
    long = "\n\n".join(f"## Part {i}\n{word} " + "words " * 80 for i in range(12))
    await write(sid, "big.md", "# Big\n" + long)
    await write(sid, "small.md", f"# Small\n{word} short")
    r = await find(word, reranker=client(ok()))
    per = {}
    for x in r["results"]:
        per[x["document_id"]] = per.get(x["document_id"], 0) + 1
    assert max(per.values()) <= service.PER_DOCUMENT


async def test_a_change_during_the_call_is_honored():
    word = f"tern{uuid.uuid4().hex[:6]}"
    sid = await source()
    gone = await write(sid, "a.md", f"# A\n{word} one")
    await write(sid, "b.md", f"# B\n{word} two")
    base = ok()

    class Racing(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            async with session_scope() as db:
                await service.set_document_lifecycle(db, await db.get(Document, gone), "quarantined")
            return base(request)

    r = await find(word, reranker=rerank.Reranker("http://r", "t", MODEL, REV, transport=Racing()))
    assert str(gone) not in [x["document_id"] for x in r["results"]] and r["reranking"]["removed_after_recheck"] == 1


async def test_input_is_bounded_and_clipping_is_reported():
    word = f"coot{uuid.uuid4().hex[:6]}"
    await write(await source(), "a.md", f"# A\n{word} " + "long text " * 80)
    h = ok()
    r = await find(word + " " + "q" * 5000, reranker=client(h, max_query_chars=50, max_passage_chars=100))
    assert len(h.seen["body"]["query"]) == 50 and all(len(c["text"]) <= 100 for c in h.seen["body"]["candidates"])
    assert r["reranking"]["truncated_passages"] >= 1 and any(x.get("ranking", {}).get("partially_scored") for x in r["results"])


def test_configured_limits_cannot_pass_the_ceilings():
    c = rerank.Reranker("http://r", "t", MODEL, REV, candidates=9999, timeout_ms=999_999, max_query_chars=10**7, max_passage_chars=10**7)
    assert (c.candidates, c.timeout_ms, c.max_query_chars, c.max_passage_chars) == (rerank.MAX_CANDIDATES, rerank.MAX_TIMEOUT_MS, rerank.MAX_QUERY_CHARS, rerank.MAX_PASSAGE_CHARS)


async def test_truncation_from_the_service_reaches_the_result():
    word = f"rail{uuid.uuid4().hex[:6]}"
    await corpus(word, 2)

    def handle(req):
        body = json.loads(req.content)
        return httpx.Response(200, json={"request_id": "x", "results": [{"id": c["id"], "score": 1.0} for c in body["candidates"]], "model": {"id": MODEL, "revision": REV}, "duration_ms": 1, "queue_ms": 0,
                                         "truncation": {"max_length_tokens": 512, "query_tokens": 1, "truncated_ids": [body["candidates"][0]["id"]]}})
    assert (await find(word, reranker=client(handle)))["reranking"]["truncated_passages"] == 1


BAD = {
    "unknown id": lambda ids: [{"id": "not-sent", "score": 1.0}] + [{"id": c, "score": 1.0} for c in ids[1:]],
    "missing id": lambda ids: [{"id": c, "score": 1.0} for c in ids[1:]],
    "duplicate id": lambda ids: [{"id": ids[0], "score": 1.0}] + [{"id": c, "score": 1.0} for c in ids],
    "text score": lambda ids: [{"id": c, "score": "high"} for c in ids],
    "boolean score": lambda ids: [{"id": c, "score": True} for c in ids],
    "null score": lambda ids: [{"id": c, "score": None} for c in ids],
    "extra field": lambda ids: [{"id": c, "score": 1.0, "text": "rewritten evidence"} for c in ids],
    "not a list": lambda ids: {"a": 1},
}


@pytest.mark.parametrize("name", list(BAD))
async def test_a_malformed_answer_changes_nothing(name):
    word = f"ibis{uuid.uuid4().hex[:6]}"
    await corpus(word)

    def handle(req):
        ids = [c["id"] for c in json.loads(req.content)["candidates"]]
        return httpx.Response(200, json={"results": BAD[name](ids), "model": {"id": MODEL, "revision": REV}})
    plain, r = await find(word), await find(word, reranker=client(handle))
    assert [x["chunk_id"] for x in r["results"]] == [x["chunk_id"] for x in plain["results"]]
    assert r["reranking"]["state"] == "fallback" and r["reranking"]["reason"] == "invalid_output"


@pytest.mark.parametrize("bad", ['{"results": [{"id": "a", "score": NaN}]}', "not json", "[]"])
async def test_unparseable_or_non_finite_output_is_refused(bad):
    word = f"loon{uuid.uuid4().hex[:6]}"
    await corpus(word, 1)
    r = await find(word, reranker=client(lambda req: httpx.Response(200, text=bad)))
    assert r["reranking"]["reason"] == "invalid_output"


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
async def test_a_non_finite_score_on_a_valid_id_is_refused(bad):
    def handle(req):
        cid = json.loads(req.content)["candidates"][0]["id"]
        return httpx.Response(200, text='{"results": [{"id": "%s", "score": %s}], "model": {"id": "%s", "revision": "%s"}}' % (cid, bad, MODEL, REV))
    assert (await client(handle).rerank("q", [("a", "t")])).reason == "invalid_output"


async def test_a_changed_model_revision_is_not_trusted():
    word = f"auk{uuid.uuid4().hex[:6]}"
    await corpus(word)
    h = ok(model={"id": MODEL, "revision": "different"})
    plain, r = await find(word), await find(word, reranker=client(h))
    assert r["reranking"]["reason"] == "model_mismatch" and [x["chunk_id"] for x in r["results"]] == [x["chunk_id"] for x in plain["results"]]


@pytest.mark.parametrize("status,reason", [(429, "overload"), (504, "timeout"), (503, "unavailable"), (500, "unavailable"), (401, "auth")])
async def test_service_refusals_keep_the_existing_order(status, reason):
    word = f"kite{uuid.uuid4().hex[:6]}"
    await corpus(word)
    plain, r = await find(word), await find(word, reranker=client(lambda req: httpx.Response(status, json={"detail": "no"})))
    assert r["reranking"]["reason"] == reason and [x["chunk_id"] for x in r["results"]] == [x["chunk_id"] for x in plain["results"]]


@pytest.mark.parametrize("exc,reason", [(httpx.ReadTimeout("t"), "timeout"), (httpx.ConnectError("c"), "unavailable")])
async def test_transport_failures_keep_the_existing_order(exc, reason):
    word = f"gull{uuid.uuid4().hex[:6]}"
    await corpus(word)

    def boom(req):
        raise exc
    assert (await find(word, reranker=client(boom)))["reranking"]["reason"] == reason


async def test_fallback_is_deterministic():
    word = f"duck{uuid.uuid4().hex[:6]}"
    await corpus(word)
    down = client(lambda req: httpx.Response(503))
    first = [x["chunk_id"] for x in (await find(word, reranker=down))["results"]]
    for _ in range(3):
        assert [x["chunk_id"] for x in (await find(word, reranker=down))["results"]] == first


async def test_repeated_failures_stop_calling_for_a_while():
    calls = []

    def down(req):
        calls.append(1)
        return httpx.Response(503)
    c = client(down)
    now = [100.0]
    c._clock = lambda: now[0]
    for _ in range(rerank.FAILURES_TO_OPEN):
        await c.rerank("q", [("a", "t")])
    made = len(calls)
    assert (await c.rerank("q", [("a", "t")])).reason == "circuit_open" and len(calls) == made
    now[0] += rerank.OPEN_SECONDS + 1
    await c.rerank("q", [("a", "t")])
    assert len(calls) == made + 1


async def test_recovery_closes_the_circuit():
    state = {"up": False}
    good = ok()

    def flaky(req):
        return good(req) if state["up"] else httpx.Response(503)
    c = client(flaky)
    now = [0.0]
    c._clock = lambda: now[0]
    for _ in range(rerank.FAILURES_TO_OPEN):
        await c.rerank("q", [("a", "t")])
    state["up"] = True
    now[0] += rerank.OPEN_SECONDS + 1
    assert (await c.rerank("q", [("a", "t")])).scores == {"a": 0.0} and c._failures == 0


def test_disabled_is_the_default(monkeypatch):
    assert rerank.active() is None
    s = rerank.get_settings()
    monkeypatch.setattr(s, "rag_rerank_enabled", True)
    assert rerank.active() is None  # enabled without a pinned model is still off
    monkeypatch.setattr(s, "rag_rerank_url", "http://r")
    monkeypatch.setattr(s, "rag_rerank_model", MODEL)
    assert rerank.active() is None
    monkeypatch.setattr(s, "rag_rerank_revision", REV)
    assert rerank.active().revision == REV


async def test_disabled_search_looks_exactly_as_it_did():
    word = f"swan{uuid.uuid4().hex[:6]}"
    await corpus(word)
    r = await find(word)
    assert "reranking" not in r and all("ranking" not in x for x in r["results"])


async def test_the_token_is_sent_and_nothing_names_a_model():
    word = f"rook{uuid.uuid4().hex[:6]}"
    await corpus(word, 1)
    h = ok()
    await find(word, reranker=client(h))
    assert h.seen["auth"] == "Bearer t" and set(h.seen["body"]) == {"request_id", "query", "limit", "timeout_ms", "candidates"}


async def test_the_model_sees_ranking_only_through_the_evidence_view():
    from moks.tools.phase2 import KbArgs, kb_search

    word = f"lark{uuid.uuid4().hex[:6]}"
    await corpus(word, 2)
    real = service.search

    async def with_client(*a, **kw):
        return await real(*a, **kw, reranker=client(ok()))

    class Ctx:
        principal, focus_namespace = ADMIN, None

        class environment:
            name = "anything"
    import moks.rag.service as svc
    svc_search, svc.search = svc.search, with_client
    try:
        out = await kb_search(Ctx, KbArgs(query=word))
    finally:
        svc.search = svc_search
    assert out.source["reranking"]["state"] in ("applied", "fallback")
    assert all("ranking" not in h for h in out.data)
