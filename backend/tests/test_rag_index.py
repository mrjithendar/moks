"""Embedding index lifecycle: dimensions first, keyword-only honesty, recoverable reindex."""
import json
import uuid

import httpx
import pytest
from sqlalchemy import delete, select

from moks.connectors.base import ConnectorError
from moks.db import session_scope
from moks.llm.providers import LLMError
from moks.models import EmbeddingIndex, KnowledgeSource, LLMConfig
from moks.rag import service
from moks.rag.backends import OpenSearchBackend


class FakeOS:
    def __init__(self):
        self.indexes: dict[str, dict] = {}
        self.docs: dict[str, list] = {}
        self.deleted: list[str] = []
        self.fail_delete = False
        self.fail_bulk = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        name = req.url.path.strip("/").split("/")[0]
        if req.method == "HEAD":
            return httpx.Response(200 if name in self.indexes else 404)
        if req.method == "PUT":
            self.indexes[name] = json.loads(req.content)
            return httpx.Response(200, json={})
        if req.method == "GET" and "_mapping" in req.url.path:
            return httpx.Response(200, json={name: {"mappings": self.indexes[name]["mappings"]}})
        if req.method == "DELETE":
            if self.fail_delete:
                return httpx.Response(503, text="busy")
            self.deleted.append(name)
            self.indexes.pop(name, None)
            return httpx.Response(200, json={})
        if "_bulk" in req.url.path:
            if self.fail_bulk:
                return httpx.Response(200, json={"errors": True, "items": [{"index": {"error": "x"}}]})
            for line in req.content.decode().splitlines()[1::2]:
                self.docs.setdefault("all", []).append(json.loads(line))
            return httpx.Response(200, json={"errors": False})
        return httpx.Response(200, json={"hits": {"hits": []}})


class Embedder:
    def __init__(self, size=4, fail=False):
        self.size, self.fail = size, fail

    async def embed(self, texts):
        if self.fail:
            raise LLMError("unavailable", "model down")
        return [[0.1] * self.size for _ in texts]


@pytest.fixture
async def os_(monkeypatch):
    fake = FakeOS()
    monkeypatch.setattr(service, "_opensearch", OpenSearchBackend("http://os:9200", transport=httpx.MockTransport(fake)))
    monkeypatch.setattr(service, "backend_name", lambda: "opensearch")
    async with session_scope() as db:
        await db.execute(delete(EmbeddingIndex).where(EmbeddingIndex.backend == "opensearch"))
        await db.execute(delete(LLMConfig).where(LLMConfig.purpose == "embedding"))
    yield fake
    async with session_scope() as db:
        await db.execute(delete(EmbeddingIndex).where(EmbeddingIndex.backend == "opensearch"))
        await db.execute(delete(LLMConfig).where(LLMConfig.purpose == "embedding"))


async def _model(monkeypatch, size=4, configured=None, fail=False, name=None):
    async with session_scope() as db:
        for c in (await db.execute(select(LLMConfig).where(LLMConfig.purpose == "embedding"))).scalars():
            c.is_active = False
        await db.flush()
        db.add(LLMConfig(name=name or f"e-{uuid.uuid4().hex[:6]}", purpose="embedding", provider="openai", model=f"m{size}",
                         embedding_dimensions=configured, is_active=True))
    monkeypatch.setattr(service, "provider_for", lambda cfg: Embedder(size, fail))


async def _source():
    async with session_scope() as db:
        src = KnowledgeSource(name=f"ix-{uuid.uuid4().hex[:6]}", type="upload", config={})
        db.add(src)
        await db.flush()
        return src.id


async def _doc(sid, text="# Runbook\nrestart the thing"):
    async with session_scope() as db:
        src = await db.get(KnowledgeSource, sid)
        await service.index_document(db, src, f"{uuid.uuid4().hex}.md", text, revision=None, url=None)


async def _active():
    async with session_scope() as db:
        return await service.active_index(db)


async def test_vector_mapping_uses_the_models_real_dimensions(os_, monkeypatch):
    await _model(monkeypatch, size=4)  # none configured: the old code built a mapping with no vector field
    await _doc(await _source())
    idx = await _active()
    assert idx.dimensions == 4
    assert os_.indexes[idx.index_name]["mappings"]["properties"]["embedding"]["dimension"] == 4
    assert all(len(d["embedding"]) == 4 for d in os_.docs["all"])


async def test_configured_dimensions_must_match_the_model(os_, monkeypatch):
    await _model(monkeypatch, size=4, configured=8)
    await _doc(await _source())  # creation survives, as keyword-only, and says why
    idx = await _active()
    assert idx.dimensions is None and "returns 4" in idx.error
    assert "embedding" not in os_.indexes[idx.index_name]["mappings"]["properties"]


async def test_embedding_outage_at_first_index_is_explicit_keyword_only(os_, monkeypatch):
    await _model(monkeypatch, fail=True)
    await _doc(await _source())
    idx = await _active()
    assert idx.model_key == "keyword-only" and idx.dimensions is None and "unavailable" in idx.error
    assert all("embedding" not in d for d in os_.docs["all"])
    async with session_scope() as db:
        s = await service.stats(db)
    assert s["index"]["mode"] == "keyword-only"


async def test_a_model_returning_another_size_keeps_the_document_keyword_searchable(os_, monkeypatch):
    await _model(monkeypatch, size=4)
    sid = await _source()
    await _doc(sid)
    monkeypatch.setattr(service, "provider_for", lambda cfg: Embedder(6))
    await _doc(sid, "# Second\ntext")
    assert "embedding" not in os_.docs["all"][-1]


async def test_incompatible_existing_mapping_is_refused(os_):
    os_.indexes["x"] = {"mappings": {"properties": {"embedding": {"dimension": 3}}}}
    with pytest.raises(ConnectorError, match="dimension 3"):
        await service.opensearch().ensure_index("x", 4)


async def test_reindex_switches_then_cleans_up_after_commit(os_, monkeypatch):
    await _doc(await _source())
    old = await _active()
    await _model(monkeypatch, size=4)
    async with session_scope() as db:
        assert (await service.stats(db))["index"]["reindex_needed"]
        r = await service.reindex(db)
    assert r["replaced"] == old.index_name and old.index_name in os_.indexes  # not deleted by the switch itself
    assert (await _active()).dimensions == 4
    assert await service.cleanup_retired() == 1 and old.index_name in os_.deleted


async def test_failed_cleanup_is_retried_and_never_touches_the_active_index(os_, monkeypatch):
    await _doc(await _source())
    old = await _active()
    await _model(monkeypatch, size=4)
    async with session_scope() as db:
        await service.reindex(db)
    os_.fail_delete = True
    assert await service.cleanup_retired() == 0
    os_.fail_delete = False
    assert await service.cleanup_retired() == 1 and os_.deleted == [old.index_name]
    assert (await _active()).index_name in os_.indexes


async def test_failed_build_keeps_the_old_index_serving(os_, monkeypatch):
    await _doc(await _source())
    old = await _active()
    await _model(monkeypatch, size=4)
    os_.fail_bulk = True
    with pytest.raises(ConnectorError):
        async with session_scope() as db:
            await service.reindex(db)
    assert (await _active()).id == old.id
    async with session_scope() as db:
        failed = (await db.execute(select(EmbeddingIndex).where(EmbeddingIndex.status == "failed", EmbeddingIndex.backend == "opensearch"))).scalar_one()
    os_.fail_bulk = False
    await service.cleanup_retired()
    assert failed.index_name in os_.deleted and old.index_name not in os_.deleted


async def test_concurrent_reindex_is_refused(os_, monkeypatch):
    await _doc(await _source())
    await _model(monkeypatch, size=4)
    from datetime import datetime, timezone

    async with session_scope() as db:
        db.add(EmbeddingIndex(backend="opensearch", model_key="x", index_name="running", status="building", heartbeat_at=datetime.now(timezone.utc)))
    with pytest.raises(ConnectorError, match="already running"):
        async with session_scope() as db:
            await service.reindex(db)


async def test_a_dead_build_does_not_block_reindex_forever(os_, monkeypatch):
    await _doc(await _source())
    await _model(monkeypatch, size=4)
    from datetime import datetime, timedelta, timezone

    async with session_scope() as db:
        db.add(EmbeddingIndex(backend="opensearch", model_key="x", index_name="dead", status="building",
                              heartbeat_at=datetime.now(timezone.utc) - timedelta(hours=2)))
    async with session_scope() as db:
        r = await service.reindex(db)
    assert r["model"].endswith("m4/default")
