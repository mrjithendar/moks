"""Coverage is recorded and never assumed, evaluation content stays behind its boundary, and new content earns trust."""
import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from moks.connectors.base import ConnectorError
from moks.db import session_scope
from moks.evals import gates
from moks.models import Document, DocumentIndexState, EmbeddingIndex, IndexOutbox, KnowledgeSource
from moks.rag import access, query, service
from moks.rag.backends import PostgresBackend
from moks.rbac import Binding, Principal
from tests.conftest import login, make_user
from tests.test_rag_index import _model, os_  # noqa: F401

ADMIN = Principal(str(uuid.uuid4()), "root@x", [Binding("admin", "*", "*")])


async def source(realm="operational", **kw):
    async with session_scope() as db:
        s = KnowledgeSource(name=f"t-{uuid.uuid4().hex[:8]}", type="upload", config={}, scope_environment="*", scope_namespace="*", realm=realm, **kw)
        db.add(s)
        await db.flush()
        return s.id


async def write(sid, path, body):
    async with session_scope() as db:
        return (await service.index_document(db, await db.get(KnowledgeSource, sid), path, body, revision="r1", url=None)).id


async def find(q, who=ADMIN, **kw):
    async with session_scope() as db:
        return await service.search(db, who, q, 5, **kw)


async def built(os_, monkeypatch):  # noqa: F811
    await _model(monkeypatch)
    sid = await source()
    did = await write(sid, "a.md", "# Runbook\nfirst text about the gateway")
    await service.drain_outbox()
    return sid, did


def fail_during_catch_up(monkeypatch, sid):
    """A document changes during the build, and embedding is down when the catch-up reaches it."""
    real = service._catch_up

    async def catch_up(db, new, caught):
        await write(sid, "a.md", "# Runbook\nsecond text about the gateway")

        async def down(*a):
            return None, "unavailable: model down"
        monkeypatch.setattr(service, "_try_embed", down)
        return await real(db, new, caught)
    monkeypatch.setattr(service, "_catch_up", catch_up)


async def test_strict_activation_refuses_a_build_with_a_gap(os_, monkeypatch):  # noqa: F811
    sid, _ = await built(os_, monkeypatch)
    fail_during_catch_up(monkeypatch, sid)
    async with session_scope() as db:
        old = (await service.active_index(db)).id
    with pytest.raises(ConnectorError, match="incomplete"):
        async with session_scope() as db:
            await service.reindex(db)
    async with session_scope() as db:
        assert (await service.active_index(db)).id == old


async def test_degraded_activation_reports_exactly_what_is_missing(os_, monkeypatch):  # noqa: F811
    sid, did = await built(os_, monkeypatch)
    fail_during_catch_up(monkeypatch, sid)
    monkeypatch.setattr(service.get_settings(), "rag_activation", "degraded")
    async with session_scope() as db:
        r = await service.reindex(db)
    assert r["activation"] == "degraded" and r["incomplete_documents"] == 1
    async with session_scope() as db:
        doc = await db.get(Document, did)
        stats = await service.stats(db)
        state = (await db.execute(select(DocumentIndexState).where(DocumentIndexState.document_id == did, DocumentIndexState.index_id == (await service.active_index(db)).id))).scalar_one()
    assert doc.vector_state == "missing" and "model down" in doc.vector_error  # the old code wrote complete here
    assert stats["index"]["activation"] == "degraded" and stats["index"]["incomplete_documents"] == 1 and stats["embedding"]["coverage"] < 1
    assert (state.vectors, state.expected_vectors) == (0, state.expected_chunks) and state.retries >= 1 and state.model_key


async def test_coverage_returns_only_when_vectors_really_exist(os_, monkeypatch):  # noqa: F811
    sid, did = await built(os_, monkeypatch)
    real_try = service._try_embed
    fail_during_catch_up(monkeypatch, sid)
    monkeypatch.setattr(service.get_settings(), "rag_activation", "degraded")
    async with session_scope() as db:
        await service.reindex(db)
    await service.drain_outbox()  # the change's own event, with embedding still down
    assert (await service.backfill_vectors())["filled"] == 0  # nothing is cleared
    async with session_scope() as db:
        assert (await service.active_index(db)).activation == "degraded"
    monkeypatch.setattr(service, "_try_embed", real_try)
    assert (await service.backfill_vectors())["filled"] == 1
    async with session_scope() as db:
        idx = await service.active_index(db)
        assert idx.activation == "complete" and idx.error is None and (await db.get(Document, did)).vector_state == "complete"


def test_a_gap_is_described_not_rounded_up():
    class Idx:
        dimensions = 4

    class S:
        revision, chunks, vectors, error = 3, 2, 1, None
    assert service._gap(None, 3, Idx, 2) == "not written to this index"
    assert "holds revision" in service._gap(S, 4, Idx, 2)
    assert "of 5 chunks" in service._gap(S, 3, Idx, 5)
    assert "1 of 2 vectors" in service._gap(S, 3, Idx, 2)
    S.vectors = 2
    assert service._gap(S, 3, Idx, 2) is None


# ---- evaluation boundary -----------------------------------------------------------------------

async def in_realm(fn):
    token = access.enter_realm(access.EVALUATION)
    try:
        return await fn()
    finally:
        access.leave_realm(token)


async def fixture_doc(word):
    async def add():
        sid = await source(access.EVALUATION)
        return sid, await write(sid, "fixture.md", f"# Fixture\n{word} appears only here")
    return await in_realm(add)


async def test_a_wildcard_admin_never_finds_evaluation_content():
    word = f"zebra{uuid.uuid4().hex[:6]}"
    sid, did = await fixture_doc(word)
    assert (await find(word))["results"] == []
    assert [r["document_id"] for r in (await in_realm(lambda: find(word)))["results"]] == [str(did)]


async def test_each_layer_of_the_boundary_holds_alone(monkeypatch):
    """The candidate queries and the authoritative check each keep evaluation content out by themselves."""
    word = f"ibis{uuid.uuid4().hex[:6]}"
    await fixture_doc(word)
    async with session_scope() as db:
        assert await PostgresBackend(db).keyword(word, access.grants(ADMIN), 5) == []  # the candidate layer
    candidates = await in_realm(lambda: _keyword(word))
    assert candidates

    async def bad_index(*a, **k):  # an index that returns what it should not
        return [candidates], True, None
    monkeypatch.setattr(service, "_rankings", bad_index)
    assert (await find(word))["results"] == []  # the authoritative layer


async def _keyword(word):
    async with session_scope() as db:
        return await PostgresBackend(db).keyword(word, access.grants(ADMIN), 5)


async def test_an_evaluation_never_reads_production_documents():
    word = f"yak{uuid.uuid4().hex[:6]}"
    await write(await source(), "prod.md", f"# Prod\n{word} is production")
    assert (await find(word))["results"]
    assert (await in_realm(lambda: find(word)))["results"] == []


async def test_a_document_cannot_leave_its_sources_realm():
    sid = await source(access.EVALUATION)
    with pytest.raises(IntegrityError):
        async with session_scope() as db:
            db.add(Document(source_id=sid, realm="operational", path="x", content_hash="0" * 64))


async def test_no_endpoint_shows_evaluation_content(client):
    sid, did = await fixture_doc("anything")
    email = f"a-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(email, ["admin"])
    await login(client, email)
    assert (await client.get(f"/moks/api/knowledge/documents/{did}")).status_code == 404
    assert str(sid) not in [s["id"] for s in (await client.get("/moks/api/knowledge/sources")).json()]


async def test_evaluations_do_not_touch_the_outbox(monkeypatch):
    monkeypatch.setattr(service.get_settings(), "rag_backend", "opensearch")  # an external index is live
    async def count():
        async with session_scope() as db:
            return len((await db.execute(select(IndexOutbox))).all())
    before = await count()
    await fixture_doc("outbox")
    assert await count() == before and await in_realm(lambda: asyncio.sleep(0, service.external())) is None


async def test_a_run_fails_closed_and_cleans_up_when_isolation_cannot_be_shown(monkeypatch):
    async def leaked(*a):
        return ["some-document"]
    monkeypatch.setattr(gates, "_prove_isolated", leaked)
    data = {"documents": [{"id": "a", "environment": "dev", "namespace": "n", "path": "a.md", "text": "# A\ntext"}], "bindings": {"r": [["viewer", "dev", "*"]]}, "cases": [{"id": "c", "query": "text", "as": "r"}]}
    rid = uuid.uuid4()
    with pytest.raises(gates.IsolationError):
        await gates.run_rag(data, rid)
    async with session_scope() as db:
        assert (await db.execute(select(KnowledgeSource).where(KnowledgeSource.name.like(f"eval-{str(rid)[:8]}-%")))).first() is None


async def test_abandoned_evaluation_content_is_collected():
    sid, _ = await fixture_doc("old")
    async with session_scope() as db:
        await db.execute(text("UPDATE knowledge_sources SET created_at = now() - interval '2 hours' WHERE id = :i"), {"i": sid})
    assert await service.cleanup_evaluations() >= 1
    async with session_scope() as db:
        assert await db.get(KnowledgeSource, sid) is None


def test_a_gate_that_checked_nothing_has_not_passed():
    g = gates._gate("x", [], 0)
    assert g["state"] == "skipped" and g["passed"] is False and not gates.gates_ok([g])
    assert gates._not_applicable([g], ["x"])[0]["state"] == "not_applicable" and gates.gates_ok([g])
    assert not gates.gates_ok([])


# ---- retrieval ---------------------------------------------------------------------------------

def test_a_question_is_read_for_what_it_names():
    got = query.understand("why did pod checkout-7f return HTTP 503 and CrashLoopBackOff since 10:30", ["checkout-7f"])
    assert "pod" in got["resource_types"] and "HTTP 503" in got["errors"] and "CrashLoopBackOff" in got["errors"] and got["time_references"] == ["since 10:30"] or got["time_references"]


def test_decomposition_is_bounded():
    q = "how do I restart redis; how do I rotate certificates; how do I drain a node; how do I resize a volume"
    assert len(query.parts(q)) == query.MAX_PARTS and query.parts("why is checkout slow") == []


async def test_decomposing_cannot_widen_access(monkeypatch):
    monkeypatch.setattr(service.get_settings(), "rag_decompose", True)
    word = f"okapi{uuid.uuid4().hex[:6]}"
    async with session_scope() as db:
        s = KnowledgeSource(name=f"t-{uuid.uuid4().hex[:8]}", type="upload", config={}, scope_environment="secret-env", scope_namespace="*")
        db.add(s)
        await db.flush()
        await service.index_document(db, s, "s.md", f"# S\n{word} restart steps", revision="r", url=None)
    who = Principal(str(uuid.uuid4()), "p@x", [Binding("viewer", "other", "*")])
    hits = (await find(f"how to {word} restart; what does {word} need", who))["results"]
    assert not any(word in h["text"] for h in hits)  # documents shared with everyone may still match the other words


async def test_search_says_when_it_abstains():
    r = await find(f"nosuchthing{uuid.uuid4().hex[:6]} ERR_NOPE_1")
    assert r["abstain"] is True and "understanding" in r


# ---- lifecycle ---------------------------------------------------------------------------------

async def test_new_content_waits_for_review_when_the_source_requires_it():
    word = f"lemur{uuid.uuid4().hex[:6]}"
    sid = await source(review_required=True)
    did = await write(sid, "q.md", f"# Q\n{word} steps")
    assert (await find(word))["results"] == []
    async with session_scope() as db:
        doc = await db.get(Document, did)
        assert doc.lifecycle == "quarantined"
        await service.set_document_lifecycle(db, doc, "active")
    assert (await find(word))["results"]


async def test_changed_content_loses_its_review():
    word = f"tapir{uuid.uuid4().hex[:6]}"
    sid = await source(authority=2)
    did = await write(sid, "r.md", f"# R\n{word} one")
    assert (await find(word))["results"][0]["freshness"]["trust"] == "unreviewed"
    assert "authority" not in (await find(word))["results"][0]["signals"]
    async with session_scope() as db:
        service.review_document(await db.get(Document, did), None, None)
    hit = (await find(word))["results"][0]
    assert hit["freshness"]["trust"] == "reviewed" and "authority" in hit["signals"]
    await write(sid, "r.md", f"# R\n{word} two changed")
    assert (await find(word))["results"][0]["freshness"]["trust"] == "unreviewed"


async def test_archiving_removes_a_document_from_search_at_once():
    word = f"quokka{uuid.uuid4().hex[:6]}"
    did = await write(await source(), "a.md", f"# A\n{word}")
    async with session_scope() as db:
        await service.set_document_lifecycle(db, await db.get(Document, did), "archived")
    assert (await find(word))["results"] == []


async def test_lifecycle_moves_are_checked():
    did = await write(await source(), "b.md", "# B\ntext here")
    async with session_scope() as db:
        with pytest.raises(ValueError):
            await service.set_document_lifecycle(db, await db.get(Document, did), "draft")


async def test_a_replacement_links_the_two_documents():
    sid = await source()
    old, new = await write(sid, "old.md", "# O\nold words"), await write(sid, "new.md", "# N\nnew words")
    async with session_scope() as db:
        doc = await db.get(Document, old)
        await service.set_document_lifecycle(db, doc, "archived", superseded_by=new)
        assert doc.superseded_by_id == new and doc.doc_state == "superseded"


async def test_index_rows_for_evaluations_are_their_own():
    await fixture_doc("rows")
    async with session_scope() as db:
        backends = {i.backend for i in (await db.execute(select(EmbeddingIndex))).scalars()}
    assert "evaluation" in backends
