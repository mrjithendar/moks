"""OpenSearch behaviour that needs no server: configuration refusals, what a bulk response means, how
generations are named, and how outages and retention are handled. test_opensearch_real.py runs the rest for real."""
import json
import uuid

import httpx
import pytest
from sqlalchemy import text

from moks.connectors.base import ConnectorError
from moks.db import session_scope
from moks.models import Document, DocumentIndexState, EmbeddingIndex, KnowledgeSource
from moks.rag import service
from moks.rag.backends import MAPPING_VERSION, OpenSearchBackend, index_body


def backend(handler, **kw):
    return OpenSearchBackend("https://os:9200", transport=httpx.MockTransport(handler), **kw)


def test_credentials_are_refused_over_plain_http():
    with pytest.raises(ConnectorError, match="plain http"):
        OpenSearchBackend("http://moks-search:9200", "u", "p")


def test_a_ca_with_verification_off_is_a_contradiction_not_a_choice():
    with pytest.raises(ConnectorError, match="verification is off"):
        OpenSearchBackend("https://x:9200", ca_file="/etc/hostname", verify=False)


def test_an_unreadable_ca_file_stops_startup():
    with pytest.raises(ConnectorError, match="CA file"):
        OpenSearchBackend("https://x:9200", ca_file="/nonexistent/ca.crt")


def test_only_the_search_identity_is_needed_to_build_a_client():
    os_ = backend(lambda r: httpx.Response(200, json={}), username="s", password="p")
    assert os_.index_client is os_.client and os_.lifecycle_client is os_.client
    os2 = backend(lambda r: httpx.Response(200, json={}), username="s", password="p", index_auth=("i", "p"), lifecycle_auth=("l", "p"))
    assert len({os2.client, os2.index_client, os2.lifecycle_client}) == 3


def test_the_mapping_uses_keywords_for_identifiers_and_no_replica():
    body = index_body(3072, "gemini/x/3072")
    props = body["mappings"]["properties"]
    assert all(props[f]["type"] == "keyword" for f in ("chunk_id", "document_id", "source_id", "environment", "namespace", "service", "git_revision", "owner", "team", "lifecycle", "classification"))
    assert props["text"]["fields"]["exact"]["analyzer"] == "moks_identifier" and props["embedding"]["dimension"] == 3072
    assert body["settings"]["index"]["number_of_replicas"] == 0 and body["settings"]["index"]["number_of_shards"] == 1
    assert body["mappings"]["dynamic"] == "strict" and body["mappings"]["_meta"]["mapping_version"] == MAPPING_VERSION


def test_a_keyword_only_generation_has_no_vector_field():
    body = index_body(None)
    assert "embedding" not in body["mappings"]["properties"] and "knn" not in body["settings"]["index"]


ROWS = [{"chunk_id": f"c{i}", "content_revision": 2} for i in range(3)]


def bulk(*statuses):
    items = [{"index": {"status": s, **({"error": {"type": "x", "reason": "y"}} if s >= 400 else {})}} for s in statuses]
    return httpx.Response(200, json={"errors": any(s >= 400 for s in statuses), "items": items})


async def test_http_200_with_a_refused_item_is_a_failure():
    with pytest.raises(ConnectorError) as e:
        await backend(lambda r: bulk(201, 400, 201)).index_chunks("i", ROWS, None)
    assert e.value.kind == "partial" and e.value.retryable and "1 of 3" in e.value.message


async def test_a_response_that_does_not_account_for_every_item_is_a_failure():
    with pytest.raises(ConnectorError, match="2 items for a bulk of 3"):
        await backend(lambda r: bulk(201, 201)).index_chunks("i", ROWS, None)


async def test_a_version_conflict_is_a_newer_write_not_a_failure():
    got = await backend(lambda r: bulk(201, 409, 200)).index_chunks("i", ROWS, None)
    assert (got.written, got.superseded, got.failed) == (2, 1, [])


async def test_bulk_writes_fence_on_the_document_revision():
    seen = []

    def h(req):
        seen.extend(json.loads(line) for line in req.content.decode().splitlines())
        return bulk(201, 201, 201)
    await backend(h).index_chunks("i", ROWS, None)
    assert seen[0]["index"]["version"] == 2 and seen[0]["index"]["version_type"] == "external_gte" and seen[1]["has_vector"] is False


async def test_a_row_for_an_older_generation_is_not_fenced_or_extended():
    seen = []

    def h(req):
        seen.extend(json.loads(line) for line in req.content.decode().splitlines())
        return bulk(201)
    await backend(h).index_chunks("i", [{"chunk_id": "c"}], None)
    assert "version" not in seen[0]["index"] and "has_vector" not in seen[1]


async def test_undeleted_chunks_are_reported():
    def h(req):
        return httpx.Response(200, json={"deleted": 0, "version_conflicts": 2, "failures": []})
    with pytest.raises(ConnectorError) as e:
        await backend(h).delete_document("i", uuid.uuid4())
    assert e.value.kind == "partial" and e.value.retryable


@pytest.mark.parametrize("status,kind,retryable", [(401, "auth", False), (403, "auth", False), (429, "unavailable", True), (503, "unavailable", True), (400, "bad_request", False)])
async def test_status_codes_become_what_a_caller_can_act_on(status, kind, retryable):
    with pytest.raises(ConnectorError) as e:
        await backend(lambda r: httpx.Response(status, text="x")).health()
    assert e.value.kind == kind and bool(e.value.retryable) is retryable


async def test_a_partly_failed_search_is_not_an_answer():
    def h(req):
        return httpx.Response(200, json={"_shards": {"failed": 1}, "hits": {"hits": []}})
    with pytest.raises(ConnectorError, match="part of the index"):
        await backend(h).keyword("i", "q", [], 5)


async def test_a_write_block_stops_a_build_before_it_starts():
    def h(req):
        if "state/blocks" in req.url.path:
            return httpx.Response(200, json={"blocks": {"indices": {"i": {"8": {"description": "flood stage"}}}}})
        return httpx.Response(200, json={"status": "green", "nodes": {}})
    with pytest.raises(ConnectorError, match="write block"):
        await backend(h).preflight(1)


def test_generations_are_named_per_cluster(monkeypatch):
    s = service.get_settings()
    monkeypatch.setattr(s, "rag_backend", "opensearch")
    monkeypatch.setattr(s, "opensearch_cluster", "secure")
    assert service.backend_name() == "opensearch-secure" and service.is_opensearch(service.backend_name())
    monkeypatch.setattr(s, "opensearch_cluster", "")
    assert service.backend_name() == "opensearch"
    monkeypatch.setattr(s, "rag_backend", "postgres")
    monkeypatch.setattr(s, "opensearch_cluster", "secure")
    assert service.backend_name() == "postgres"


class Ext:
    keeps_old_chunks = True

    def __init__(self):
        self.calls = []

    async def ensure_index(self, name, dims, model_key=""):
        pass

    async def index_chunks(self, name, rows, embeddings):
        self.calls.append(("index", len(rows), bool(embeddings)))

    async def delete_document(self, name, document_id, keep=None):
        self.calls.append(("delete", keep))

    async def drop_index(self, name):
        self.calls.append(("drop", name))


@pytest.fixture
async def ext(monkeypatch):
    fake = Ext()
    monkeypatch.setattr(service, "external", lambda name=None: fake)
    monkeypatch.setattr(service, "backend_name", lambda: "opensearch")
    async with session_scope() as db:
        await db.execute(text("UPDATE index_outbox SET status = 'done' WHERE status <> 'done'"))
        await db.execute(text("DELETE FROM embedding_indexes WHERE backend = 'opensearch'"))
        db.add(EmbeddingIndex(backend="opensearch", model_key="m", dimensions=3, llm_config_id=None, index_name="moks-chunks-x", status="active", mapping_version=2))
    yield fake


async def _document():
    async with session_scope() as db:
        src = KnowledgeSource(name=f"c-{uuid.uuid4().hex[:6]}", type="upload", config={})
        db.add(src)
        await db.flush()
        return (await service.index_document(db, src, "a.md", "# T\n\nbody words here", revision=None, url=None)).id


async def test_a_rewrite_writes_before_it_prunes_so_a_document_is_never_absent(ext):
    await _document()
    await service.drain_outbox()
    assert [c[0] for c in ext.calls] == ["index", "delete"] and ext.calls[1][1]


async def test_a_failed_write_prunes_nothing(ext, monkeypatch):
    async def refuse(*a):
        raise ConnectorError("partial", "refused")
    monkeypatch.setattr(ext, "index_chunks", refuse)
    await _document()
    assert (await service.drain_outbox())["retry"] == 1 and ext.calls == []


async def test_an_embedding_outage_never_replaces_good_vectors_with_none(ext, monkeypatch):
    did = await _document()
    await service.drain_outbox()
    ext.calls.clear()
    async with session_scope() as db:
        idx = await service.active_index(db)
        await db.execute(text("UPDATE document_index_state SET vectors = 1, chunks = 1 WHERE document_id = :d"), {"d": did})

    async def down(*a, **k):
        return None, "unavailable: provider down"
    monkeypatch.setattr(service, "_try_embed", down)
    async with session_scope() as db:
        await service._write_document(db, await db.get(EmbeddingIndex, idx.id), did)
    assert ext.calls == []


async def test_a_new_revision_during_an_outage_is_written_keyword_only_and_says_so(ext, monkeypatch):
    async def down(*a, **k):
        return None, "unavailable: provider down"
    monkeypatch.setattr(service, "_try_embed", down)
    did = await _document()
    await service.drain_outbox()
    async with session_scope() as db:
        doc = await db.get(Document, did)
        state = await db.get(DocumentIndexState, (did, (await service.active_index(db)).id))
        assert doc.vector_state == "missing" and state.vectors == 0 and state.error == "unavailable: provider down"
        assert (await service.stats(db))["embedding"]["coverage"] != 1
    assert ext.calls[0] == ("index", 1, False)


async def test_retention_keeps_a_retired_generation_and_never_ages_out_an_active_one(ext, monkeypatch):
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(service.get_settings(), "rag_retired_retention_hours", 24)
    now = datetime.now(timezone.utc)
    async with session_scope() as db:
        db.add(EmbeddingIndex(backend="opensearch", model_key="m", index_name="moks-chunks-young", status="retired", cleanup_pending=True, retired_at=now - timedelta(hours=1)))
        db.add(EmbeddingIndex(backend="opensearch", model_key="m", index_name="moks-chunks-old", status="retired", cleanup_pending=True, retired_at=now - timedelta(hours=48)))
        await db.execute(text("UPDATE embedding_indexes SET created_at = now() - interval '5 years', cleanup_pending = true WHERE status = 'active' AND backend = 'opensearch'"))
    assert await service.cleanup_retired() == 1
    assert ext.calls == [("drop", "moks-chunks-old")]


async def test_another_clusters_generation_is_not_cleaned_from_here(ext):
    async with session_scope() as db:
        db.add(EmbeddingIndex(backend="opensearch-secure", model_key="m", index_name="moks-chunks-other", status="retired", cleanup_pending=True))
    try:
        assert await service.cleanup_retired() == 0 and ext.calls == []
    finally:
        async with session_scope() as db:
            await db.execute(text("DELETE FROM embedding_indexes WHERE backend = 'opensearch-secure'"))


async def test_a_generation_from_another_model_cannot_be_activated(ext, monkeypatch):
    from types import SimpleNamespace
    async with session_scope() as db:
        db.add(EmbeddingIndex(backend="opensearch", model_key="old/model/768", dimensions=768, index_name="moks-chunks-prior", status="retired"))

    async def cfg(db):
        return SimpleNamespace(provider="new", model="model", embedding_dimensions=1024)
    monkeypatch.setattr(service, "embedding_config", cfg)
    with pytest.raises(ConnectorError, match="would not compare"):
        async with session_scope() as db:
            await service.activate_generation(db, "moks-chunks-prior")

