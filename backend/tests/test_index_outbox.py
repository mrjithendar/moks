"""The external index follows PostgreSQL after commit, and can never get ahead of it."""
import asyncio
import uuid

import pytest
from sqlalchemy import func, select, text, update

from moks.connectors.base import ConnectorError
from moks.db import session_scope
from moks.models import Document, EmbeddingIndex, IndexOutbox, KnowledgeSource
from moks.rag import service


class Fake:
    def __init__(self):
        self.docs: dict[str, dict[str, set]] = {}
        self.calls: list[tuple] = []
        self.fail = 0
        self.before_write = None

    async def ensure_index(self, name, dims):
        self.docs.setdefault(name, {})

    def _maybe_fail(self):
        if self.fail:
            self.fail -= 1
            raise ConnectorError("unavailable", "external index is down")

    async def delete_document(self, name, document_id):
        self._maybe_fail()
        self.calls.append(("delete", name, str(document_id)))
        self.docs.setdefault(name, {}).pop(str(document_id), None)

    async def index_chunks(self, name, rows, embeddings):
        self._maybe_fail()
        for r in rows:
            self.docs.setdefault(name, {}).setdefault(r["document_id"], set()).add(r["text"])
        self.calls.append(("index", name, len(rows)))
        if self.before_write:
            hook, self.before_write = self.before_write, None
            await hook()

    async def drop_index(self, name):
        self.docs.pop(name, None)


@pytest.fixture
async def ext(monkeypatch):
    fake = Fake()
    monkeypatch.setattr(service, "external", lambda name=None: fake)
    monkeypatch.setattr(service, "backend_name", lambda: "opensearch")
    async with session_scope() as db:
        await db.execute(text("DELETE FROM index_outbox"))
        await db.execute(text("DELETE FROM embedding_indexes WHERE backend = 'opensearch'"))
        db.add(EmbeddingIndex(backend="opensearch", model_key="keyword-only", index_name="active-index", status="active"))
    yield fake


async def _source():
    async with session_scope() as db:
        src = KnowledgeSource(name=f"o-{uuid.uuid4().hex[:6]}", type="upload", config={})
        db.add(src)
        await db.flush()
        return src.id


async def _write(sid, path="a.md", body="# Runbook\nfirst version of the text"):
    async with session_scope() as db:
        src = await db.get(KnowledgeSource, sid)
        return (await service.index_document(db, src, path, body, revision=None, url=None)).id


async def _doc(did):
    async with session_scope() as db:
        return await db.get(Document, did)


async def _pending():
    async with session_scope() as db:
        return (await db.execute(select(func.count()).select_from(IndexOutbox).where(IndexOutbox.status == "pending"))).scalar_one()


async def test_nothing_reaches_the_index_before_commit(ext):
    sid = await _source()
    await _write(sid)
    assert ext.calls == [] and await _pending() == 1


async def test_a_rolled_back_update_leaves_search_content_intact(ext):
    sid = await _source()
    did = await _write(sid)
    await service.drain_outbox()
    before = dict(ext.docs["active-index"])
    ext.calls.clear()
    with pytest.raises(RuntimeError):
        async with session_scope() as db:
            src = await db.get(KnowledgeSource, sid)
            await service.index_document(db, src, "a.md", "# Runbook\nreplacement", revision=None, url=None)
            raise RuntimeError("the transaction fails after the change")
    with pytest.raises(RuntimeError):
        async with session_scope() as db:
            await service.remove_document(db, await db.get(Document, did))
            raise RuntimeError("the delete fails too")
    await service.drain_outbox()
    assert ext.calls == [] and ext.docs["active-index"] == before
    assert (await _doc(did)).status == "active"


async def test_external_failure_retries_with_backoff_then_applies(ext):
    sid = await _source()
    did = await _write(sid)
    ext.fail = 1
    assert (await service.drain_outbox())["retry"] == 1
    assert (await _doc(did)).indexed_revision == 0
    async with session_scope() as db:
        ev = (await db.execute(select(IndexOutbox))).scalars().one()
        assert ev.attempts == 1 and ev.error and ev.status == "pending"
        ev.next_attempt_at = func.now()
    assert (await service.drain_outbox())["applied"] == 1
    assert (await _doc(did)).indexed_revision == 1


async def test_repeated_failure_goes_dead_and_is_visible_and_retryable(ext, monkeypatch):
    monkeypatch.setattr(service, "OUTBOX_MAX_ATTEMPTS", 2)
    sid = await _source()
    await _write(sid)
    for _ in range(2):
        ext.fail = 5
        await service.drain_outbox()
        async with session_scope() as db:
            await db.execute(update(IndexOutbox).where(IndexOutbox.status == "pending").values(next_attempt_at=func.now()))
    async with session_scope() as db:
        assert (await service.stats(db))["outbox"]["dead"] == 1
    ext.fail = 0
    assert await service.retry_dead() == 1
    assert (await service.drain_outbox())["applied"] == 1


async def test_duplicate_event_is_harmless(ext):
    sid = await _source()
    did = await _write(sid)
    await service.drain_outbox()
    ext.calls.clear()
    async with session_scope() as db:
        await db.execute(update(IndexOutbox).values(status="pending"))  # delivered twice
    assert (await service.drain_outbox())["skipped"] == 1
    assert ext.calls == [] and (await _doc(did)).indexed_revision == 1


async def test_out_of_order_old_event_is_rejected(ext):
    sid = await _source()
    did = await _write(sid)
    await _write(sid, body="# Runbook\nsecond version of the text")
    async with session_scope() as db:  # the newer event comes first
        await db.execute(text("UPDATE index_outbox SET next_attempt_at = now() + interval '1 hour' WHERE revision = 1"))
    assert (await service.drain_outbox())["applied"] == 1
    async with session_scope() as db:
        await db.execute(text("UPDATE index_outbox SET next_attempt_at = now() WHERE revision = 1"))
    assert (await service.drain_outbox())["skipped"] == 1
    assert (await _doc(did)).indexed_revision == 2
    texts = ext.docs["active-index"][str(did)]
    assert any("second" in t for t in texts) and not any("first" in t for t in texts)


async def test_deletion_reaches_the_index(ext):
    sid = await _source()
    did = await _write(sid)
    await service.drain_outbox()
    async with session_scope() as db:
        await service.remove_document(db, await db.get(Document, did))
    await service.drain_outbox()
    assert str(did) not in ext.docs["active-index"]


async def test_a_crash_while_applying_leaves_the_event_pending(ext):
    sid = await _source()
    did = await _write(sid)

    async def crash(*a):
        raise asyncio.CancelledError  # the process dies here; its transaction never commits
    ext.index_chunks = crash
    with pytest.raises(asyncio.CancelledError):
        await service.drain_outbox()
    assert await _pending() == 1 and (await _doc(did)).indexed_revision == 0


async def test_reconcile_requeues_a_document_left_behind(ext):
    sid = await _source()
    did = await _write(sid)
    async with session_scope() as db:
        await db.execute(text("DELETE FROM index_outbox"))
    assert await service.reconcile_index() >= 1
    await service.drain_outbox(10000)
    doc = await _doc(did)
    assert doc.indexed_revision == doc.content_revision == 2


async def test_reindex_includes_updates_and_deletions_made_during_the_build(ext, monkeypatch):
    sid = await _source()
    changed = await _write(sid, "changed.md")
    removed = await _write(sid, "removed.md")
    await service.drain_outbox()

    async def concurrent():
        await _write(sid, "changed.md", "# Runbook\nsecond version of the text")
        async with session_scope() as db:
            await service.remove_document(db, await db.get(Document, removed))
    ext.before_write = concurrent
    ext.calls.clear()
    async with session_scope() as db:
        result = await service.reindex(db)
    new = result["index"]
    assert str(removed) not in ext.docs[new]
    assert not any("first" in t for t in ext.docs[new][str(changed)]) and any("second" in t for t in ext.docs[new][str(changed)])
    await service.drain_outbox()  # the old pending events now land on the new active index, harmlessly
    assert str(removed) not in ext.docs[new]


async def test_failed_build_keeps_the_active_index_and_cleans_only_itself(ext):
    sid = await _source()
    await _write(sid)
    await service.drain_outbox()
    ext.fail = 1
    with pytest.raises(ConnectorError):
        async with session_scope() as db:
            await service.reindex(db)
    async with session_scope() as db:
        assert (await service.active_index(db)).index_name == "active-index"
    await service.cleanup_retired()
    assert "active-index" in ext.docs
