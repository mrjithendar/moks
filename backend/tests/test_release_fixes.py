"""Editing knowledge sources, partial syncs, embedding connection tests, and waiting out rate limits."""
import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from moks import jobs
from moks.api import admin
from moks.db import session_scope
from moks.llm.providers import LLMError
from moks.models import AuditEvent, Document, KnowledgeSource, LLMConfig
from moks.rag import service
from moks.rag.backends import OpenSearchBackend
from tests.conftest import login, make_user
from tests.test_rag import FakeGitHub, LimitedGitHub, _source, _use


async def _admin(client):
    tag = uuid.uuid4().hex[:6]
    await make_user(f"a-{tag}@example.com", ["admin"])
    return await login(client, f"a-{tag}@example.com"), tag


async def _synced(monkeypatch, gh):
    from moks.rag import ingest

    sid = await _source("edit", type_="github", config={"repository": "acme/ops"})
    _use(monkeypatch, gh)
    async with session_scope() as db:
        await ingest.sync_github(db, await db.get(KnowledgeSource, sid))
    return sid


async def _docs(sid):
    async with session_scope() as db:
        return (await db.execute(select(Document).where(Document.source_id == sid))).scalars().all()


async def test_editing_a_source_keeps_its_documents_and_audits_only_the_reference(client, monkeypatch):
    h, _ = await _admin(client)
    sid = await _synced(monkeypatch, FakeGitHub({"docs/a.md": "# A", "docs/b.md": "# B"}))
    r = await client.put(f"/moks/api/knowledge/sources/{sid}", json={"credential_ref": {"type": "env", "name": "MOKS_CRED_GITHUB_TOKEN"}, "owner": "sre", "include": ["docs/**"]}, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["documents"] == {"active": 2} and out["owner"] == "sre" and out["config"]["include"] == ["docs/**"] and out["config"]["repository"] == "acme/ops"
    assert out["credential"].startswith("env:MOKS_CRED_GITHUB_TOKEN")
    assert len(await _docs(sid)) == 2
    async with session_scope() as db:
        ev = (await db.execute(select(AuditEvent).where(AuditEvent.action == "knowledge.source_update", AuditEvent.resource_id == str(sid)))).scalar_one()
    assert ev.details["changed"] == ["credential_ref", "include", "owner"]


async def test_repository_name_and_type_cannot_change(client, monkeypatch):
    h, _ = await _admin(client)
    sid = await _source("fixed", type_="github", config={"repository": "acme/ops"})
    for body in ({"repository": "acme/other"}, {"name": "x"}, {"type": "upload"}):
        assert (await client.put(f"/moks/api/knowledge/sources/{sid}", json=body, headers=h)).status_code == 422


async def test_upload_sources_refuse_github_settings(client):
    h, _ = await _admin(client)
    sid = await _source("up", type_="upload")
    assert (await client.put(f"/moks/api/knowledge/sources/{sid}", json={"include": ["x"]}, headers=h)).status_code == 422
    assert (await client.put(f"/moks/api/knowledge/sources/{sid}", json={"owner": "sre"}, headers=h)).status_code == 200


async def test_only_connector_admins_can_edit(client):
    tag = uuid.uuid4().hex[:6]
    await make_user(f"o-{tag}@example.com", ["operator:*:*"])
    h = await login(client, f"o-{tag}@example.com")
    sid = await _source("nope", type_="github", config={"repository": "acme/ops"})
    assert (await client.put(f"/moks/api/knowledge/sources/{sid}", json={"owner": "x"}, headers=h)).status_code == 403


async def test_changing_scope_moves_documents_so_access_filters_follow(client, monkeypatch):
    h, _ = await _admin(client)
    sid = await _synced(monkeypatch, FakeGitHub({"docs/a.md": f"# A\nzebra{uuid.uuid4().hex[:6]}"}))
    r = await client.put(f"/moks/api/knowledge/sources/{sid}", json={"scope_environment": "prod", "scope_namespace": "shop"}, headers=h)
    assert r.status_code == 200
    d = (await _docs(sid))[0]
    assert (d.environment, d.namespace) == ("prod", "shop")


async def test_a_document_with_its_own_scope_keeps_it(client):
    h, _ = await _admin(client)
    sid = await _source("own", type_="upload")
    async with session_scope() as db:
        src = await db.get(KnowledgeSource, sid)
        await service.index_document(db, src, "a.md", "# A", revision=None, url=None, extra={"environment": "mk4", "namespace": "shop"})
    await client.put(f"/moks/api/knowledge/sources/{sid}", json={"scope_environment": "other"}, headers=h)
    d = (await _docs(sid))[0]
    assert (d.environment, d.namespace) == ("mk4", "shop")


async def test_scope_change_reaches_opensearch_and_a_failure_saves_nothing(client, monkeypatch):
    h, _ = await _admin(client)
    sent = []
    fail = {"on": False}

    def transport(req: httpx.Request) -> httpx.Response:
        if "_update_by_query" in req.url.path:
            if fail["on"]:
                return httpx.Response(503, text="busy")
            sent.append(json.loads(req.content))
        return httpx.Response(200, json={})

    monkeypatch.setattr(service, "_opensearch", OpenSearchBackend("http://os:9200", transport=httpx.MockTransport(transport)))
    monkeypatch.setattr(service, "backend_name", lambda: "opensearch")
    sid = await _source("os", type_="upload")
    async with session_scope() as db:
        from moks.models import Chunk, EmbeddingIndex

        await db.execute(EmbeddingIndex.__table__.delete().where(EmbeddingIndex.backend == "opensearch"))
        db.add(EmbeddingIndex(backend="opensearch", model_key="keyword-only", index_name="moks-chunks-x", status="active"))
        db.add(Document(source_id=sid, path="a.md", content_hash="h", kind="doc", environment="*", namespace="*", status="active"))
    fail["on"] = True
    r = await client.put(f"/moks/api/knowledge/sources/{sid}", json={"scope_environment": "prod"}, headers=h)
    assert r.status_code == 502 and "nothing was saved" in r.json()["detail"]
    async with session_scope() as db:
        assert (await db.get(KnowledgeSource, sid)).scope_environment == "*"
    fail["on"] = False
    assert (await client.put(f"/moks/api/knowledge/sources/{sid}", json={"scope_environment": "prod"}, headers=h)).status_code == 200
    assert sent[0]["script"]["params"] == {"e": "prod", "n": "*"}
    async with session_scope() as db:
        await db.execute(EmbeddingIndex.__table__.delete().where(EmbeddingIndex.backend == "opensearch"))


async def test_a_sync_that_skips_files_reports_partial_with_the_count(client, monkeypatch):
    h, _ = await _admin(client)
    gh = LimitedGitHub({"docs/a.md": "# A", "docs/b.md": "# B"}, 1, lambda: httpx.Response(404, json={"message": "gone"}))
    sid = await _source("part", type_="github", config={"repository": "acme/ops"})
    _use(monkeypatch, gh)
    async with session_scope() as db:
        await jobs.handlers()["knowledge_sync"](db, {"source_id": str(sid)})
    row = next(s for s in (await client.get("/moks/api/knowledge/sources")).json() if s["id"] == str(sid))
    assert row["last_sync_status"] == "partial" and row["last_counts"]["failed"] == 1 and "1 files could not be read" in row["last_error"]
    assert row["last_success_at"] is None


class Embedder:
    def __init__(self, cfg=None, size=8, error=None):
        self.size, self.error = size, error

    async def embed(self, texts):
        if self.error:
            raise self.error
        return [[0.1] * self.size for _ in texts]


async def _embedding_config(**kw):
    async with session_scope() as db:
        c = LLMConfig(name=f"t-{uuid.uuid4().hex[:6]}", purpose="embedding", provider="gemini", model="gemini-embedding-001", is_active=False, **kw)
        db.add(c)
        await db.flush()
        return c.id


async def _test(client, h, lid):
    return await client.post(f"/moks/api/admin/llm/{lid}/test", headers=h)


async def test_embedding_test_makes_a_real_call_and_reports_dimensions(client, monkeypatch):
    h, _ = await _admin(client)
    lid = await _embedding_config()
    monkeypatch.setattr(admin, "provider_for", lambda c: Embedder(size=3072))
    r = (await _test(client, h, lid)).json()
    assert r["ok"] and r["provider"] == "gemini" and r["model"] == "gemini-embedding-001" and r["dimensions"] == 3072
    assert "Phase" not in r["message"]


@pytest.mark.parametrize("error,kind", [(LLMError("auth", "gemini rejected the credentials (HTTP 403)"), "auth"), (LLMError("rate_limited", "429"), "rate_limited"), (LLMError("config", "no key"), "config")])
async def test_embedding_test_reports_failures(client, monkeypatch, error, kind):
    h, _ = await _admin(client)
    lid = await _embedding_config()
    monkeypatch.setattr(admin, "provider_for", lambda c: Embedder(error=error))
    r = (await _test(client, h, lid)).json()
    assert r["ok"] is False and r["message"].startswith(kind)


async def test_embedding_test_flags_a_size_mismatch_and_an_empty_answer(client, monkeypatch):
    h, _ = await _admin(client)
    lid = await _embedding_config(embedding_dimensions=768)
    monkeypatch.setattr(admin, "provider_for", lambda c: Embedder(size=3072))
    r = (await _test(client, h, lid)).json()
    assert r["ok"] is False and "768" in r["message"] and r["dimensions"] == 3072
    monkeypatch.setattr(admin, "provider_for", lambda c: Embedder(size=0))
    assert (await _test(client, h, lid)).json()["ok"] is False


async def test_a_reindex_waits_out_a_rate_limit_and_gives_up_eventually(monkeypatch):
    monkeypatch.setattr(service, "EMBED_BACKOFF_SECONDS", 0)

    class Flaky:
        calls = 0

        async def embed(self, texts):
            Flaky.calls += 1
            if Flaky.calls < 3:
                raise LLMError("rate_limited", "429")
            return [[0.1]]
    assert await service._embed_batch(Flaky(), ["x"], 5) == [[0.1]] and Flaky.calls == 3

    class Always:
        calls = 0

        async def embed(self, texts):
            Always.calls += 1
            raise LLMError("rate_limited", "429")
    with pytest.raises(LLMError):
        await service._embed_batch(Always(), ["x"], 3)
    assert Always.calls == 3

    class Auth:
        calls = 0

        async def embed(self, texts):
            Auth.calls += 1
            raise LLMError("auth", "denied")
    with pytest.raises(LLMError):
        await service._embed_batch(Auth(), ["x"], 5)
    assert Auth.calls == 1  # only a rate limit is worth waiting for


