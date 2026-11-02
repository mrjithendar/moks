"""Against a real OpenSearch with the chart's own security configuration: TLS, identities, mappings and
retrieval. Skipped unless MOKS_TEST_OPENSEARCH_URL is set, with MOKS_TEST_OPENSEARCH_CREDS naming the directory
`scripts/opensearch-secrets.sh --files` wrote. docs/TESTING.md has the setup. Every index here is a throwaway
named moks-chunks-test-*, removed afterwards, and never in a cluster that serves anyone."""
import json
import os
import ssl
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, text

from moks.connectors.base import ConnectorError
from moks.db import session_scope
from moks.llm.providers import LLMError
from moks.models import Document, EmbeddingIndex, KnowledgeSource, LLMConfig
from moks.rag import access, service
from moks.rag.backends import MAPPING_VERSION, OpenSearchBackend

URL = os.environ.get("MOKS_TEST_OPENSEARCH_URL", "")
CREDS = Path(os.environ.get("MOKS_TEST_OPENSEARCH_CREDS", "/nonexistent"))
pytestmark = pytest.mark.skipif(not URL, reason="needs a real OpenSearch")
EVERYONE = [access.Grant("*", "*")]


def passwords() -> dict[str, str]:
    return dict(line.strip().split("=", 1) for line in (CREDS / "passwords").read_text().splitlines())


def client(**kw) -> OpenSearchBackend:
    p = passwords()
    return OpenSearchBackend(URL, "moks_search", p["search"], ca_file=str(CREDS / "ca.crt"), index_auth=("moks_indexer", p["index"]),
                             lifecycle_auth=("moks_lifecycle", p["lifecycle"]), **kw)


@pytest.fixture
async def os_():
    backend = client()
    yield backend
    await backend.aclose()


@pytest.fixture
async def name(os_):
    index = f"moks-chunks-test-{uuid.uuid4().hex[:8]}"
    yield index
    await os_.drop_index(index)


def row(document, text_, chunk=None, environment="prod", namespace="shop", revision=1, **extra):
    return {"chunk_id": chunk or str(uuid.uuid4()), "document_id": document, "source_id": str(uuid.uuid4()), "realm": "operational", "environment": environment, "namespace": namespace,
            "repository": "r", "service": "", "path": "docs/a.md", "heading": "h", "text": text_, "content_revision": revision, "identifiers": service.identifiers(text_)} | extra


async def seed(os_, name, rows, dims=None, vectors=None):
    await os_.ensure_index(name, dims)
    return await os_.index_chunks(name, rows, vectors)


# TLS and identity

async def test_a_certificate_from_another_authority_is_refused():
    wrong = OpenSearchBackend(URL, "moks_search", passwords()["search"], transport=None)  # the system trust store
    with pytest.raises(ConnectorError) as e:
        await wrong.health()
    assert e.value.kind == "unavailable" and "ConnectError" in e.value.message
    await wrong.aclose()


async def test_requests_without_a_login_or_with_a_wrong_one_are_refused():
    tls = ssl.create_default_context(cafile=str(CREDS / "ca.crt"))
    async with httpx.AsyncClient(verify=tls) as c:
        assert (await c.get(f"{URL}/_cluster/health")).status_code == 401
        assert (await c.get(f"{URL}/_cluster/health", auth=("moks_search", "wrong"))).status_code == 401


async def test_the_chart_probe_path_needs_no_login_and_says_nothing_useful():
    tls = ssl.create_default_context(cafile=str(CREDS / "ca.crt"))
    async with httpx.AsyncClient(verify=tls) as c:
        got = await c.get(f"{URL}/_plugins/_security/health")
    assert got.status_code == 200 and got.json()["status"] == "UP"


async def test_each_identity_can_do_its_job_and_no_more(os_, name):
    await seed(os_, name, [row(str(uuid.uuid4()), "first text")])
    search = os_.search_client
    assert (await search.post(f"/{name}/_search", json={})).status_code == 200
    assert (await search.post(f"/{name}/_doc", json={"text": "x"})).status_code == 403
    assert (await search.delete(f"/{name}")).status_code == 403
    assert (await search.get("/_cluster/health")).status_code == 403
    indexer = os_.index_client
    assert (await indexer.delete(f"/{name}")).status_code == 403
    assert (await indexer.put(f"/moks-chunks-test-{uuid.uuid4().hex[:6]}")).status_code == 403
    lifecycle = os_.lifecycle_client
    assert (await lifecycle.put("/not-a-moks-index")).status_code == 403
    assert (await lifecycle.post("/.opendistro_security/_search", json={})).status_code == 403
    p = passwords()
    tls = ssl.create_default_context(cafile=str(CREDS / "ca.crt"))
    async with httpx.AsyncClient(base_url=URL, verify=tls, auth=("moks_monitor", p["monitor"])) as monitor:
        assert (await monitor.get("/_cluster/health")).status_code == 200
        assert (await monitor.post(f"/{name}/_search", json={})).status_code == 403  # sees numbers, never text


async def test_drop_refuses_anything_but_a_knowledge_index(os_):
    with pytest.raises(ConnectorError, match="not a MOKS knowledge index"):
        await os_.drop_index(".opendistro_security")


# Mapping

async def test_the_mapping_is_strict_and_versioned(os_, name):
    await os_.ensure_index(name, 4, "provider/model/4")
    mapping = await os_.mapping(name)
    assert mapping["dynamic"] == "strict" and mapping["_meta"]["mapping_version"] == MAPPING_VERSION and mapping["_meta"]["model_key"] == "provider/model/4"
    assert mapping["properties"]["embedding"]["dimension"] == 4 and mapping["properties"]["document_id"]["type"] == "keyword"
    with pytest.raises(ConnectorError, match="bulk items"):
        await os_.index_chunks(name, [row(str(uuid.uuid4()), "x", unplanned_field="y")], None)


async def test_an_existing_index_with_another_size_is_refused(os_, name):
    await os_.ensure_index(name, 4)
    with pytest.raises(ConnectorError, match="vector dimension 4, expected 8"):
        await os_.ensure_index(name, 8)


async def test_a_vector_of_the_wrong_size_is_refused_item_by_item(os_, name):
    await os_.ensure_index(name, 4)
    good, bad = row(str(uuid.uuid4()), "good"), row(str(uuid.uuid4()), "bad")
    with pytest.raises(ConnectorError) as e:
        await os_.index_chunks(name, [good, bad], [[0.1, 0.2, 0.3, 0.4], [0.1, 0.2]])
    assert e.value.kind == "partial" and "1 of 2" in e.value.message and e.value.retryable
    assert await os_.count(name) == 1  # the other item stuck, which is why a retry rewrites the whole document


# Identifiers and hybrid ranking

@pytest.mark.parametrize("identifier", ["CrashLoopBackOff", "MOKS_RAG_BACKEND", "moks-api-797c6cb777", "deploy/helm/moks/values.yaml", "HTTP-503", "10.244.1.90:9200"])
async def test_exact_identifiers_are_found_whatever_the_case(os_, name, identifier):
    target = row(str(uuid.uuid4()), f"When this happens check {identifier} first and restart.")
    noise = [row(str(uuid.uuid4()), "When this happens check the rollout status first and restart.") for _ in range(30)]
    await seed(os_, name, [target, *noise])
    ranked, _ = await os_.keyword(name, identifier, EVERYONE, 5, None, [identifier])
    assert ranked[0] == target["chunk_id"]
    ranked, _ = await os_.keyword(name, identifier.lower(), EVERYONE, 5, None, [identifier])
    assert ranked[0] == target["chunk_id"]


async def test_prose_is_stemmed_so_english_questions_still_match(os_, name):
    target = row(str(uuid.uuid4()), "The pods were restarting because the volumes filled up.")
    await seed(os_, name, [target, row(str(uuid.uuid4()), "Unrelated text about billing.")])
    assert (await os_.keyword(name, "pod restarts", EVERYONE, 5))[0][0] == target["chunk_id"]


async def test_vector_search_returns_the_nearest_within_the_filter(os_, name):
    near_hidden, near_visible, far = (row(str(uuid.uuid4()), "a", environment="prod", namespace="secret"), row(str(uuid.uuid4()), "b"), row(str(uuid.uuid4()), "c"))
    await seed(os_, name, [near_hidden, near_visible, far], 3, [[1, 0, 0], [0.9, 0.1, 0], [0, 0, 1]])
    ranked, _ = await os_.vector(name, [1, 0, 0], [access.Grant("prod", "shop")], 2)
    assert near_hidden["chunk_id"] not in ranked and ranked[0] == near_visible["chunk_id"]  # filtered during the search, so it did not use up k


# Authorization

async def test_paired_bindings_never_cross(os_, name):
    a, b, crossed, shared = (row(str(uuid.uuid4()), "needle", environment="prod", namespace="team-a"), row(str(uuid.uuid4()), "needle", environment="dev", namespace="team-b"),
                             row(str(uuid.uuid4()), "needle", environment="prod", namespace="team-b"), row(str(uuid.uuid4()), "needle", environment="*", namespace="*"))
    await seed(os_, name, [a, b, crossed, shared])
    ranked, exact = await os_.keyword(name, "needle", [access.Grant("prod", "team-a"), access.Grant("dev", "team-b")], 10)
    assert exact and set(ranked) == {a["chunk_id"], b["chunk_id"], shared["chunk_id"]}


async def test_namespace_and_environment_isolate_and_wildcards_match(os_, name):
    rows = [row(str(uuid.uuid4()), "needle", environment=e, namespace=n) for e, n in [("prod-eu", "shop"), ("prod-us", "shop"), ("dev", "shop"), ("prod-eu", "bank")]]
    await seed(os_, name, rows)
    ranked, exact = await os_.keyword(name, "needle", [access.Grant("prod-*", "shop")], 10)
    assert exact and set(ranked) == {rows[0]["chunk_id"], rows[1]["chunk_id"]}
    assert (await os_.keyword(name, "needle", [access.Grant("staging", "shop")], 10))[0] == []


async def test_a_pattern_the_filter_cannot_express_is_reported_not_widened_silently(os_, name):
    rows = [row(str(uuid.uuid4()), "needle", environment="prod1"), row(str(uuid.uuid4()), "needle", environment="prod2")]
    await seed(os_, name, rows)
    ranked, exact = await os_.keyword(name, "needle", [access.Grant("prod[1]", "shop")], 10)
    assert exact is False and len(ranked) == 2  # search must treat these as suggestions; the final check decides


async def test_excluded_sources_and_documents_are_filtered_before_ranking(os_, name):
    kept, by_doc, by_source = row(str(uuid.uuid4()), "needle"), row(str(uuid.uuid4()), "needle"), row(str(uuid.uuid4()), "needle")
    await seed(os_, name, [kept, by_doc, by_source], 3, [[1, 0, 0]] * 3)
    exclude = {"documents": [by_doc["document_id"]], "sources": [by_source["source_id"]]}
    assert (await os_.keyword(name, "needle", EVERYONE, 10, exclude))[0] == [kept["chunk_id"]]
    assert (await os_.vector(name, [1, 0, 0], EVERYONE, 10, exclude))[0] == [kept["chunk_id"]]


# Indexing

async def test_bulk_partial_failure_names_what_failed_and_keeps_the_rest(os_, name):
    await os_.ensure_index(name, None)
    ok, broken = row(str(uuid.uuid4()), "fine"), row(str(uuid.uuid4()), "broken", start_line="not a number")
    with pytest.raises(ConnectorError) as e:
        await os_.index_chunks(name, [ok, broken], None)
    assert e.value.kind == "partial" and await os_.count(name) == 1


async def test_a_replay_is_harmless_and_an_older_revision_cannot_replace_a_newer(os_, name):
    document, chunk = str(uuid.uuid4()), str(uuid.uuid4())
    await seed(os_, name, [row(document, "newer words", chunk, revision=5)])
    replay = await os_.index_chunks(name, [row(document, "newer words", chunk, revision=5)], None)
    assert replay.written == 1 and replay.superseded == 0
    older = await os_.index_chunks(name, [row(document, "older words", chunk, revision=4)], None)
    assert older.superseded == 1 and not older.failed
    assert (await os_.keyword(name, "newer", EVERYONE, 5))[0] == [chunk]
    assert (await os_.keyword(name, "older", EVERYONE, 5))[0] == []


async def test_pruning_keeps_the_new_chunks_and_a_tombstone_removes_all(os_, name):
    document = str(uuid.uuid4())
    old, new = row(document, "old words", revision=1), row(document, "new words", revision=2)
    other = row(str(uuid.uuid4()), "another document")
    await seed(os_, name, [old, other])
    await os_.index_chunks(name, [new], None)
    await os_.delete_document(name, uuid.UUID(document), keep=[new["chunk_id"]])
    assert await os_.count_document(name, uuid.UUID(document)) == 1 and (await os_.keyword(name, "old", EVERYONE, 5))[0] == []
    await os_.delete_document(name, uuid.UUID(document))
    assert await os_.count_document(name, uuid.UUID(document)) == 0 and await os_.count(name) == 1


# What is live today has mapping version 1 and no login, and this code reaches it before any cutover

async def test_an_original_generation_keeps_working_with_this_code(os_, name):
    await os_._req("PUT", f"/{name}", {"settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0}}, "mappings": {"properties": {
        "chunk_id": {"type": "keyword"}, "document_id": {"type": "keyword"}, "environment": {"type": "keyword"}, "namespace": {"type": "keyword"},
        "path": {"type": "text", "fields": {"raw": {"type": "keyword"}}}, "heading": {"type": "text"}, "text": {"type": "text"}}}}, via=os_.lifecycle_client)
    document = str(uuid.uuid4())
    old_row = {"chunk_id": str(uuid.uuid4()), "document_id": document, "environment": "prod", "namespace": "shop", "path": "a.md", "heading": "h", "text": "CrashLoopBackOff text"}
    assert (await os_.index_chunks(name, [old_row], None)).written == 1
    exclude = {"sources": [str(uuid.uuid4())], "documents": [str(uuid.uuid4())]}
    assert (await os_.keyword(name, "CrashLoopBackOff", [access.Grant("prod", "shop")], 5, exclude, ["CrashLoopBackOff"]))[0] == [old_row["chunk_id"]]
    await os_.delete_document(name, uuid.UUID(document), keep=[old_row["chunk_id"]])
    assert await os_.count_document(name, uuid.UUID(document)) == 1


async def test_compare_reports_overlap_and_no_text(live, monkeypatch):
    sid = await source("prod", "shop")
    await write(sid, "docs/k.md", "# Compare heading\n\nsome words to find")
    new = (await build())["index"]
    monkeypatch.setattr(service.get_settings(), "opensearch_compare_url", URL)
    monkeypatch.setattr(service, "OpenSearchBackend", lambda *a, **k: client())
    async with session_scope() as db:
        await db.execute(text("DELETE FROM embedding_indexes WHERE backend = 'opensearch'"))
        db.add(EmbeddingIndex(backend="opensearch", model_key="keyword-only", index_name=new, status="active"))
    try:
        out = await service.compare_generations(["compare heading"])
    finally:
        async with session_scope() as db:
            await db.execute(text("DELETE FROM embedding_indexes WHERE backend = 'opensearch'"))
    assert out["queries"] == 1 and out["mean_overlap_at_10"] == 1.0 and out["new_empty_where_old_answered"] == 0
    assert set(out["rows"][0]) == {"query", "old", "new", "overlap"}


# Failure behaviour

async def test_a_missing_index_is_a_fault_not_an_empty_answer(os_):
    with pytest.raises(ConnectorError):
        await os_.keyword("moks-chunks-test-absent", "x", EVERYONE, 5)


async def test_after_a_failure_searches_stop_waiting_for_a_while(os_):
    with pytest.raises(ConnectorError):
        await os_.keyword("moks-chunks-test-absent", "x", EVERYONE, 5)
    with pytest.raises(ConnectorError, match="failed moments ago"):
        await os_.keyword("moks-chunks-test-absent", "x", EVERYONE, 5)


async def test_unreachable_is_reported_in_the_same_shape():
    gone = OpenSearchBackend("https://127.0.0.1:1", verify=False)
    state = await gone.status()
    assert state["reachable"] is False and state["error"] and state["health"] is None
    await gone.aclose()


async def test_status_reads_a_real_cluster(os_):
    state = await os_.status()
    assert state["reachable"] and state["health"] in ("green", "yellow") and state["version"] == "3.9.0"
    assert state["disk_free_bytes"] > 0 and 0 <= state["heap_used_percent"] <= 100 and state["read_only"] is False
    assert (await os_.preflight(1024))["reachable"]
    with pytest.raises(ConnectorError, match="only"):
        await os_.preflight(10 ** 18)


# Snapshots

async def test_snapshot_policy_can_be_written_twice_and_a_snapshot_restores(os_, name, tmp_path):
    repo_dir = os.environ.get("MOKS_TEST_OPENSEARCH_REPO")
    if not repo_dir:
        pytest.skip("needs a repository path the node allows (path.repo)")
    repo = f"test-{uuid.uuid4().hex[:6]}"
    policy = f"test-{uuid.uuid4().hex[:6]}"
    doc = row(str(uuid.uuid4()), "survives a restore")
    await seed(os_, name, [doc])
    life = os_.lifecycle_client
    assert (await life.put(f"/_snapshot/{repo}", json={"type": "fs", "settings": {"location": f"{repo_dir}/{repo}"}})).status_code == 200
    await os_.put_snapshot_policy(repo, policy, "0 3 * * *", 14, "30d")
    await os_.put_snapshot_policy(repo, policy, "0 4 * * *", 14, "30d")  # the second call updates
    assert (await life.put(f"/_snapshot/{repo}/s1?wait_for_completion=true", json={"indices": name, "include_global_state": False})).status_code == 200
    seen = await os_.snapshots(repo)
    assert seen["registered"] and seen["count"] == 1 and seen["latest_state"] == "SUCCESS" and seen["last_success_ms"]
    # A restore is a person's job, so it uses the human identity, into a new name.
    p = passwords()
    tls = ssl.create_default_context(cafile=str(CREDS / "ca.crt"))
    async with httpx.AsyncClient(base_url=URL, verify=tls, auth=("moks_admin", p["admin"]), timeout=30) as admin:
        restored = f"{name}-restored"
        got = await admin.post(f"/_snapshot/{repo}/s1/_restore?wait_for_completion=true", json={"indices": name, "rename_pattern": "(.+)", "rename_replacement": "$1-restored", "include_global_state": False})
        assert got.status_code == 200, got.text
        await admin.post(f"/{restored}/_refresh")
        assert (await admin.post(f"/{restored}/_count")).json()["count"] == 1
        await admin.delete(f"/{restored}")
        await admin.delete(f"/_plugins/_sm/policies/{policy}")
        await admin.delete(f"/_snapshot/{repo}/s1")
        await admin.delete(f"/_snapshot/{repo}")


# The whole path, MOKS service over real PostgreSQL and real OpenSearch

@pytest.fixture
async def live(monkeypatch):
    backend = client()
    monkeypatch.setattr(service, "_opensearch", backend)
    monkeypatch.setattr(service, "backend_name", lambda: "opensearch-test")
    async with session_scope() as db:
        await db.execute(text("UPDATE index_outbox SET status = 'done' WHERE status <> 'done'"))
        await db.execute(text("DELETE FROM embedding_indexes WHERE backend = 'opensearch-test'"))
        await db.execute(text("DELETE FROM knowledge_sources WHERE name LIKE 'real-%'"))
    yield backend
    async with session_scope() as db:
        names = (await db.execute(select(EmbeddingIndex.index_name).where(EmbeddingIndex.backend == "opensearch-test"))).scalars().all()
        await db.execute(text("DELETE FROM embedding_indexes WHERE backend = 'opensearch-test'"))
    for n in names:
        await backend.drop_index(n)
    await backend.aclose()


def principal(*bindings):
    return SimpleNamespace(user_id=str(uuid.uuid4()), bindings=[SimpleNamespace(role="viewer", environment=e, namespace=n) for e, n in bindings])


async def source(env, ns):
    async with session_scope() as db:
        src = KnowledgeSource(name=f"real-{uuid.uuid4().hex[:6]}", type="upload", config={}, scope_environment=env, scope_namespace=ns)
        db.add(src)
        await db.flush()
        return src.id


async def write(sid, path, body):
    async with session_scope() as db:
        return (await service.index_document(db, await db.get(KnowledgeSource, sid), path, body, revision="abc", url=None)).id


async def build():
    async with session_scope() as db:
        return await service.reindex(db)


async def find(who, q, **kw):
    async with session_scope() as db:
        return await service.search(db, who, q, **kw)


async def test_search_end_to_end_with_authorization_and_citations(live):
    sid, other = await source("prod", "shop"), await source("prod", "bank")
    await write(sid, "docs/redis.md", "# Redis failover\n\nRestart with REDIS_FAILOVER_KEY set.")
    await write(other, "docs/bank.md", "# Redis failover\n\nBank procedure, REDIS_FAILOVER_KEY too.")
    await build()
    r = await find(principal(("prod", "shop")), "REDIS_FAILOVER_KEY")
    assert r["mode"] == "keyword" and r["candidates"]["filtered_before_ranking"]
    assert [x["citation"]["path"] for x in r["results"]] == ["docs/redis.md"] and r["results"][0]["citation"]["revision"] == "abc"
    assert (await find(principal(("prod", "shop")), "bank procedure", namespace="bank"))["results"] == []


async def test_quarantine_stops_a_document_at_commit_without_touching_the_index(live):
    sid = await source("prod", "shop")
    did = await write(sid, "docs/q.md", "# Quarantine me\n\nwordzebra appears here")
    await build()
    who = principal(("prod", "shop"))
    assert (await find(who, "wordzebra"))["results"]
    async with session_scope() as db:
        await service.set_document_lifecycle(db, await db.get(Document, did), "quarantined")
    assert (await find(who, "wordzebra"))["results"] == []


async def test_outbox_applies_updates_and_tombstones_with_accurate_coverage(live):
    sid = await source("prod", "shop")
    did = await write(sid, "docs/o.md", "# First\n\nwordalpha lives here")
    await build()
    await write(sid, "docs/o.md", "# Second\n\nwordbravo replaced it")
    assert (await service.drain_outbox())["applied"] == 1
    who = principal(("prod", "shop"))
    assert (await find(who, "wordbravo"))["results"] and not (await find(who, "wordalpha"))["results"]
    async with session_scope() as db:
        idx = await service.active_index(db)
        name = idx.index_name
        assert await service._coverage(db, idx) == {}
    assert await live.count_document(name, did) == 1
    async with session_scope() as db:
        await service.remove_document(db, await db.get(Document, did))
    await service.drain_outbox()
    assert await live.count_document(name, did) == 0


async def test_an_opensearch_write_failure_retries_and_recovers(live, monkeypatch):
    sid = await source("prod", "shop")
    await write(sid, "docs/r.md", "# One\n\ntext")
    await build()
    await write(sid, "docs/r.md", "# Two\n\ntext changed")
    real = live.index_chunks

    async def refuse(*a):
        raise ConnectorError("partial", "refused 1 of 1 bulk items", retryable=True)
    monkeypatch.setattr(live, "index_chunks", refuse)
    assert (await service.drain_outbox())["retry"] == 1
    monkeypatch.setattr(live, "index_chunks", real)
    async with session_scope() as db:
        await db.execute(text("UPDATE index_outbox SET next_attempt_at = now() WHERE status = 'pending'"))
    assert (await service.drain_outbox())["applied"] == 1


async def test_opensearch_down_falls_back_to_postgres_and_says_so(live, monkeypatch):
    sid = await source("prod", "shop")
    await write(sid, "docs/d.md", "# Down\n\nwordcharlie appears")
    await build()
    monkeypatch.setattr(live, "search_client", httpx.AsyncClient(base_url="https://127.0.0.1:1", verify=False))
    r = await find(principal(("prod", "shop")), "wordcharlie")
    assert r["mode"] == "keyword" and "OpenSearch unavailable" in r["fallback_reason"] and r["results"]


async def test_generation_sync_recovers_changes_made_while_another_cluster_served(live):
    sid = await source("prod", "shop")
    kept = await write(sid, "docs/s1.md", "# Kept\n\nstays the same")
    gone = await write(sid, "docs/s2.md", "# Gone\n\nwill be deleted")
    await build()
    changed = await write(sid, "docs/s3.md", "# New\n\nadded while away")
    async with session_scope() as db:
        await service.remove_document(db, await db.get(Document, gone))
        await db.execute(text("UPDATE index_outbox SET status = 'done' WHERE status = 'pending'"))  # applied elsewhere, as after a cutover
    result = await service.sync_generation()
    assert result["written"] == 1 and result["removed"] == 1 and result["remaining"] == 0
    async with session_scope() as db:
        idx = await service.active_index(db)
        name = idx.index_name
    assert await live.count_document(name, changed) == 1 and await live.count_document(name, gone) == 0 and await live.count_document(name, kept) == 1


async def test_concurrent_update_during_reindex_ends_up_in_the_new_generation(live, monkeypatch):
    sid = await source("prod", "shop")
    did = await write(sid, "docs/c.md", "# Before\n\nwordbefore")
    await build()
    real = live.index_chunks

    async def mid_build(index, rows, embeddings):
        out = await real(index, rows, embeddings)
        monkeypatch.setattr(live, "index_chunks", real)
        await write(sid, "docs/c.md", "# After\n\nwordafter")
        return out
    monkeypatch.setattr(live, "index_chunks", mid_build)
    await build()
    await service.drain_outbox()
    who = principal(("prod", "shop"))
    assert (await find(who, "wordafter"))["results"] and not (await find(who, "wordbefore"))["results"]
    async with session_scope() as db:
        idx = await service.active_index(db)
        assert await service._coverage(db, idx) == {}
        assert await live.count_document(idx.index_name, did) == 1


async def test_a_retained_generation_can_be_activated_and_a_kept_one_is_not_cleaned(live, monkeypatch):
    sid = await source("prod", "shop")
    await write(sid, "docs/a.md", "# A\n\ntext")
    first = (await build())["index"]
    await build()
    monkeypatch.setattr(service.get_settings(), "rag_retired_retention_hours", 24)
    assert await service.cleanup_retired() == 0 and await live.mapping(first) is not None
    async with session_scope() as db:
        r = await service.activate_generation(db, first)
    assert r["active"] == first
    await service.sync_generation()
    async with session_scope() as db:
        assert (await service.active_index(db)).index_name == first
    monkeypatch.setattr(service.get_settings(), "rag_retired_retention_hours", 0)
    assert await service.cleanup_retired() == 1


async def test_validation_passes_on_a_good_generation_and_fails_on_a_tampered_one(live):
    sid = await source("prod", "shop")
    await write(sid, "docs/v.md", "# Valid\n\nThe key CrashLoopBackOff matters here.")
    built = await build()
    report = await service.validate_generation()
    assert report["ok"], json.dumps(report, indent=1)
    assert report["checks"]["search_identity_cannot_write"]["ok"] is True and report["checks"]["scope_filter"]["ok"]
    await live.index_chunks(built["index"], [row(str(uuid.uuid4()), "not in postgres")], None)
    assert (await service.validate_generation())["checks"]["chunk_count"]["ok"] is False


async def test_a_standby_build_leaves_what_search_uses_alone(live, monkeypatch):
    sid = await source("prod", "shop")
    did = await write(sid, "docs/st.md", "# Standby\n\ntext")
    async with session_scope() as db:
        await db.execute(text("UPDATE documents SET vector_state = 'missing' WHERE id = :d"), {"d": did})
    monkeypatch.setattr(service.get_settings(), "opensearch_standby", True)
    await build()
    async with session_scope() as db:
        assert (await db.get(Document, did)).vector_state == "missing"
    monkeypatch.setattr(service.get_settings(), "opensearch_standby", False)
    await service.adopt_generation()
    async with session_scope() as db:
        assert (await db.get(Document, did)).vector_state == "not_applicable"


class Embedder:
    """Stands in for the provider; the vectors go to the real k-NN index."""
    down = False

    async def embed(self, cfg, texts):
        if self.down:
            raise LLMError("unavailable", "provider down")
        return [[float((hash(t) >> i) & 7) + 1 for i in (0, 3, 6, 9)] for t in texts]


@pytest.fixture
async def embedder(live, monkeypatch):
    provider = Embedder()

    async def fake(p, cfg, texts, **k):
        return await provider.embed(cfg, texts)
    monkeypatch.setattr(service.budget, "embed", fake)
    monkeypatch.setattr(service, "provider_for", lambda cfg: provider)
    async with session_scope() as db:
        await db.execute(text("UPDATE llm_configs SET is_active = false WHERE purpose = 'embedding'"))
        cfg = LLMConfig(name=f"real-{uuid.uuid4().hex[:6]}", purpose="embedding", provider="openai", model="m", endpoint="http://x/v1", is_active=True, embedding_dimensions=4)
        db.add(cfg)
        await db.flush()
        cid = cfg.id
    yield provider
    async with session_scope() as db:
        await db.execute(text("DELETE FROM embedding_indexes WHERE backend = 'opensearch-test'"))
        await db.delete(await db.get(LLMConfig, cid))


async def test_hybrid_search_and_an_embedding_outage_through_recovery(embedder):
    sid = await source("prod", "shop")
    await write(sid, "docs/h1.md", "# Hybrid\n\nwordecho with vectors")
    built = await build()
    report = await service.validate_generation()
    assert report["ok"] and report["checks"]["vector_count"]["ok"] and report["checks"]["vector_search"]["ok"], report
    who = principal(("prod", "shop"))
    assert (await find(who, "wordecho"))["mode"] == "hybrid"

    embedder.down = True
    r = await find(who, "wordecho")
    assert r["mode"] == "keyword" and "embedding failed" in r["fallback_reason"] and r["results"]
    did = await write(sid, "docs/h2.md", "# Outage\n\nwordfoxtrot written while embedding is down")
    await service.drain_outbox()
    assert (await find(who, "wordfoxtrot"))["results"]  # searchable by keyword now
    async with session_scope() as db:
        assert (await db.get(Document, did)).vector_state == "missing"
        stats = await service.stats(db)
        assert stats["embedding"]["coverage"] < 1 and stats["index"]["incomplete_documents"] >= 1  # not called complete
    report = await service.validate_generation()
    assert report["checks"]["coverage"]["ok"] is False and report["ok"] is False
    assert await live_vectors(built["index"], did) == 0

    assert (await service.backfill_vectors())["failed"] == 1  # still down: stops rather than spending budget
    embedder.down = False
    assert (await service.backfill_vectors())["filled"] == 1
    async with session_scope() as db:
        assert (await db.get(Document, did)).vector_state == "complete"
    assert await live_vectors(built["index"], did) == 1


async def live_vectors(name, document_id):
    return await service.opensearch().count(name, {"bool": {"filter": [{"exists": {"field": "embedding"}}, {"term": {"document_id": str(document_id)}}]}})


async def test_an_embedding_model_of_another_size_is_refused_before_it_reaches_the_index(embedder):
    sid = await source("prod", "shop")
    await write(sid, "docs/z.md", "# Z\n\nbody")
    built = await build()

    async def wider(cfg, texts):
        return [[1.0] * 8 for _ in texts]
    embedder.embed = wider
    did = await write(sid, "docs/z.md", "# Z\n\nchanged body")
    await service.drain_outbox()
    async with session_scope() as db:
        doc = await db.get(Document, did)
        assert doc.vector_state == "missing" and "dimensions" in doc.vector_error
    assert await live_vectors(built["index"], did) == 0


async def test_probe_records_state_for_the_api(live, monkeypatch):
    monkeypatch.setattr(service.get_settings(), "opensearch_url", URL)
    monkeypatch.setattr(service.get_settings(), "opensearch_username", "moks_search")
    assert (await service.probe_backend())["reachable"]
    async with session_scope() as db:
        report = await service.backend_report(db)
    assert report["probe"]["health"] == "green" and report["probe"]["version"] == "3.9.0" and report["tls_verified"] and report["authenticated"]
    assert service.SEARCH_BACKEND.labels("opensearch-test", "up")._value.get() == 1
