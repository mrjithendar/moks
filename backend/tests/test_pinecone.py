"""The Pinecone backend against a stand-in for its 2026-07 REST API."""
import json
import uuid

import httpx
import pytest
from sqlalchemy import delete, select

from moks.connectors.base import ConnectorError
from moks.db import session_scope
from moks.llm.providers import LLMError
from moks.models import Document, EmbeddingIndex, KnowledgeSource, LLMConfig
from moks.rag import service
from moks.rag.backends import PineconeBackend
from moks.rbac import Binding, Principal

HOST = "idx.svc.pinecone.io"


class FakePinecone:
    def __init__(self):
        self.indexes: dict[str, dict] = {}
        self.vectors: dict[str, dict] = {}
        self.requests: list[tuple] = []
        self.headers: dict = {}
        self.ready_after = 0
        self.limited = 0
        self.status = None

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.headers = dict(req.headers)
        body = json.loads(req.content) if req.content else {}
        self.requests.append((req.method, req.url.path, body))
        if self.status:
            return httpx.Response(self.status, json={"message": "no"})
        if self.limited:
            self.limited -= 1
            return httpx.Response(429, text="slow down")
        path = req.url.path
        if req.url.host == "api.pinecone.io":
            name = path.rsplit("/", 1)[-1]
            if req.method == "POST":
                self.indexes[body["name"]] = body
                return httpx.Response(201, json=self._info(body["name"]))
            if req.method == "DELETE":
                return httpx.Response(202 if self.indexes.pop(name, None) else 404)
            if name in self.indexes:
                return httpx.Response(200, json=self._info(name))
            return httpx.Response(404, json={"error": "not found"})
        if path.startswith("/vectors/") and "_values" not in next(iter(self.indexes.values()))["schema"]["fields"]:
            return httpx.Response(400, json={"code": 3, "message": "This index has a document schema, so writes must go through the documents API."})
        if path == "/vectors/upsert":
            for v in body["vectors"]:
                self.vectors[v["id"]] = v
            return httpx.Response(200, json={"upsertedCount": len(body["vectors"])})
        if path == "/vectors/delete":
            doc = body["filter"]["document_id"]["$eq"]
            self.vectors = {k: v for k, v in self.vectors.items() if v["metadata"]["document_id"] != doc}
            return httpx.Response(200)
        if path == "/vectors/update":
            for v in self.vectors.values():
                if v["metadata"]["document_id"] == body["filter"]["document_id"]["$eq"]:
                    v["metadata"].update(body["setMetadata"])
            return httpx.Response(200, json={})
        if path == "/query":
            def allowed(v):
                return all(v["metadata"][k] in c[k]["$in"] for c in body.get("filter", {}).get("$and", []) for k in c)
            return httpx.Response(200, json={"matches": [{"id": k} for k, v in self.vectors.items() if allowed(v)][:body["topK"]]})
        return httpx.Response(404)

    def _info(self, name):
        ready = self.ready_after <= 0
        self.ready_after -= 1
        return {"name": name, "host": HOST, "status": {"ready": ready}, "schema": self.indexes[name]["schema"]}


def backend(fake):
    b = PineconeBackend("test-key", transport=httpx.MockTransport(fake))
    b.POLL_SECONDS = 0.001
    return b


def rows(n=1):
    return [{"chunk_id": str(uuid.uuid4()), "document_id": "d1", "environment": "mk4", "namespace": "shop", "text": "SECRET CHUNK TEXT", "path": "docs/a.md", "heading": "h", "repository": "r", "service": ""} for _ in range(n)]


def test_a_missing_key_is_refused():
    with pytest.raises(ConnectorError, match="MOKS_CRED_PINECONE_API_KEY"):
        PineconeBackend(None)


async def test_creates_the_index_in_the_current_format_and_waits_until_ready():
    fake = FakePinecone()
    fake.ready_after = 2
    b = backend(fake)
    await b.ensure_index("moks-chunks-a", 3072)
    created = next(r for r in fake.requests if r[0] == "POST")[2]
    assert created["schema"]["fields"] == {"_values": {"type": "dense_vector", "dimension": 3072, "metric": "cosine"}}
    assert created["deployment"] == {"deployment_type": "managed", "cloud": "aws", "region": "us-east-1"}
    assert fake.headers["x-pinecone-api-version"] == "2026-07" and fake.headers["api-key"] == "test-key"


async def test_an_index_that_never_becomes_ready_is_an_error():
    fake = FakePinecone()
    fake.ready_after = 10_000
    b = backend(fake)
    b.READY_SECONDS = 0.02
    b.POLL_SECONDS = 0.005
    with pytest.raises(ConnectorError, match="not ready"):
        await b.ensure_index("x", 8)


async def test_an_existing_index_with_other_dimensions_is_refused():
    fake = FakePinecone()
    b = backend(fake)
    await b.ensure_index("x", 8)
    with pytest.raises(ConnectorError, match="dimension 8"):
        await b.ensure_index("x", 16)


async def test_keyword_only_creates_nothing():
    fake = FakePinecone()
    await backend(fake).ensure_index("x", None)
    assert not fake.requests


async def test_no_chunk_text_ever_leaves():
    fake = FakePinecone()
    b = backend(fake)
    await b.ensure_index("x", 2)
    await b.index_chunks("x", rows(60), [[0.1, 0.2]] * 60)
    upserts = [r for r in fake.requests if r[1] == "/vectors/upsert"]
    assert len(fake.vectors) == 60 and len(upserts) == 3 and all(len(r[2]["vectors"]) <= b.UPSERT_BATCH for r in upserts)
    assert "SECRET CHUNK TEXT" not in json.dumps(fake.requests) and "docs/a.md" not in json.dumps(fake.requests)
    assert set(next(iter(fake.vectors.values()))["metadata"]) == {"document_id", "environment", "namespace"}


async def test_query_applies_scope_and_returns_ids():
    fake = FakePinecone()
    b = backend(fake)
    await b.ensure_index("x", 2)
    a, c = rows(), rows()
    c[0]["environment"] = "other"
    await b.index_chunks("x", a + c, [[0.1, 0.2]] * 2)
    assert await b.vector("x", [0.1, 0.2], ["mk4"], ["shop"], 5) == [a[0]["chunk_id"]]
    query = next(r for r in fake.requests if r[1] == "/query")[2]
    assert query["topK"] == 5 and query["filter"]["$and"][0] == {"environment": {"$in": ["*", "mk4"]}}
    assert query["includeValues"] is False


async def test_delete_and_rescope_work_by_document():
    fake = FakePinecone()
    b = backend(fake)
    await b.ensure_index("x", 2)
    await b.index_chunks("x", rows(2), [[0.1, 0.2]] * 2)
    await b.update_scope("x", "d1", "prod", "pay")
    assert all(v["metadata"]["environment"] == "prod" for v in fake.vectors.values())
    await b.delete_document("x", "d1")
    assert not fake.vectors


async def test_a_rate_limit_is_waited_out_and_auth_is_reported(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr("moks.rag.backends.asyncio.sleep", instant)
    fake = FakePinecone()
    b = backend(fake)
    await b.ensure_index("x", 2)
    fake.limited = 2
    await b.index_chunks("x", rows(), [[0.1, 0.2]])
    assert len(fake.vectors) == 1
    fake.limited = 99
    with pytest.raises(ConnectorError):
        await b.index_chunks("x", rows(), [[0.1, 0.2]])
    fake.limited, fake.status = 0, 401
    with pytest.raises(ConnectorError, match="rejected the credentials"):
        await b.index_chunks("x", rows(), [[0.1, 0.2]])


async def test_dropping_a_missing_index_is_fine():
    await backend(FakePinecone()).drop_index("gone")


class Embedder:
    async def embed(self, texts):
        return [[0.1] * 4 for _ in texts]


@pytest.fixture
async def live(monkeypatch):
    fake = FakePinecone()
    monkeypatch.setattr(service, "_pinecone", backend(fake))
    monkeypatch.setattr(service, "backend_name", lambda: "pinecone")
    monkeypatch.setattr(service, "provider_for", lambda cfg: Embedder())
    async with session_scope() as db:
        await db.execute(delete(EmbeddingIndex).where(EmbeddingIndex.backend == "pinecone"))
        await db.execute(delete(LLMConfig).where(LLMConfig.purpose == "embedding"))
        db.add(LLMConfig(name=f"e-{uuid.uuid4().hex[:6]}", purpose="embedding", provider="gemini", model="m", is_active=True))
    yield fake
    async with session_scope() as db:
        await db.execute(delete(EmbeddingIndex).where(EmbeddingIndex.backend == "pinecone"))
        await db.execute(delete(LLMConfig).where(LLMConfig.purpose == "embedding"))


async def _index(text, env="*", ns="*"):
    async with session_scope() as db:
        src = KnowledgeSource(name=f"pc-{uuid.uuid4().hex[:6]}", type="upload", config={}, scope_environment=env, scope_namespace=ns)
        db.add(src)
        await db.flush()
        doc = await service.index_document(db, src, f"{uuid.uuid4().hex}.md", text, revision=None, url=None)
    await service.drain_outbox()
    return doc, src


async def test_indexing_and_hybrid_search_with_access_rechecked(live):
    tag = uuid.uuid4().hex[:8]
    await _index(f"# Runbook\nwombat{tag} restart steps", env="mk4", ns="shop")
    assert live.vectors and "wombat" not in json.dumps(live.requests)
    allowed = Principal(str(uuid.uuid4()), "a@x", [Binding("viewer", "mk4", "shop")])
    denied = Principal(str(uuid.uuid4()), "b@x", [Binding("viewer", "other", "shop")])
    async with session_scope() as db:
        hit = await service.search(db, allowed, f"wombat{tag}")
        miss = await service.search(db, denied, f"wombat{tag}")
    assert hit["mode"] == "hybrid" and hit["results"] and not miss["results"]


async def test_search_survives_a_pinecone_outage_as_keyword_only(live):
    tag = uuid.uuid4().hex[:8]
    await _index(f"# Doc\nnarwhal{tag}")
    live.status = 503
    who = Principal(str(uuid.uuid4()), "a@x", [Binding("viewer", "*", "*")])
    async with session_scope() as db:
        r = await service.search(db, who, f"narwhal{tag}")
    assert r["mode"] == "keyword" and r["results"]


async def test_reindex_builds_beside_the_old_index_and_cleans_up_after(live):
    await _index("# First\nsome text")
    async with session_scope() as db:
        old = await service.active_index(db)
    async with session_scope() as db:
        result = await service.reindex(db)
    assert result["replaced"] == old.index_name and old.index_name in live.indexes and result["index"] in live.indexes
    assert await service.cleanup_retired() == 1 and old.index_name not in live.indexes


async def test_a_removed_document_leaves_pinecone(live):
    doc, _ = await _index("# Gone\ntext")
    assert live.vectors
    async with session_scope() as db:
        row = (await db.execute(select(Document).where(Document.id == doc.id))).scalar_one()
        await service.remove_document(db, row)
    await service.drain_outbox()
    assert not live.vectors


async def test_embedding_failure_still_indexes_for_keyword_search(live, monkeypatch):
    class Down:
        async def embed(self, texts):
            raise LLMError("unavailable", "down")
    await _index("# Works\nfirst")
    monkeypatch.setattr(service, "provider_for", lambda cfg: Down())
    before = len(live.vectors)
    doc, _ = await _index("# Keyword only\nsecond")
    assert doc.status == "active" and len(live.vectors) == before
