"""Access, ingestion, citations, index consistency, retrieval quality and source governance."""
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, text

from moks.connectors import git as gitmod
from moks.connectors.base import ConnectorError
from moks.db import session_scope
from moks.llm.providers import LLMError
from moks.models import Chunk, Document, EmbeddingIndex, IndexOutbox, KnowledgeSource, KnowledgeSyncItem
from moks.rag import access, chunking, ingest, service
from moks.rbac import Binding, Principal
from tests.conftest import login, make_user
from tests.test_rag import FakeGitHub


def person(*bindings):
    return Principal(str(uuid.uuid4()), "p@x", [Binding(r, e, n) for r, e, n in bindings])


async def source(env="*", ns="*", type_="upload", config=None, **kw):
    async with session_scope() as db:
        s = KnowledgeSource(name=f"g-{uuid.uuid4().hex[:8]}", type=type_, scope_environment=env, scope_namespace=ns, config=config or {}, **kw)
        db.add(s)
        await db.flush()
        return s.id


async def doc(sid, path, body, **extra):
    async with session_scope() as db:
        return (await service.index_document(db, await db.get(KnowledgeSource, sid), path, body, revision=extra.pop("revision", None), url=None, extra=extra)).id


async def find(principal, q, k=5, **kw):
    async with session_scope() as db:
        return await service.search(db, principal, q, k, **kw)


# ---- access ------------------------------------------------------------------------------------


async def test_bindings_keep_environment_and_namespace_together():
    tag = uuid.uuid4().hex[:6]
    prod, dev = f"prod-{tag}", f"dev-{tag}"
    word = f"walrus{tag}"
    a, b = await source(prod, "team-a"), await source(prod, "team-b")
    c, d = await source(dev, "team-b"), await source(dev, "team-a")
    for sid, path in ((a, "a.md"), (b, "b.md"), (c, "c.md"), (d, "d.md")):
        await doc(sid, path, f"# {word}\n{word} {path}")
    r = await find(person(("viewer", prod, "team-a"), ("viewer", dev, "team-b")), word)
    assert sorted(x["citation"]["path"] for x in r["results"]) == ["a.md", "c.md"]


async def test_unreadable_documents_cannot_crowd_out_readable_ones():
    tag = uuid.uuid4().hex[:6]
    env, word = f"crowd-{tag}", f"narwhal{tag}"
    secret = await source(env, "team-b")
    for i in range(60):
        await doc(secret, f"s{i}.md", f"# {word}\n{word} {word} {word} {word} version {i}")
    mine = await source(env, "team-a")
    await doc(mine, "mine.md", f"# Notes\nA single mention of {word} among other text about something else entirely.")
    r = await find(person(("viewer", env, "team-a")), word, k=3)
    assert [x["citation"]["path"] for x in r["results"]] == ["mine.md"]


async def test_wildcard_bindings_match_as_patterns_in_the_query():
    tag = uuid.uuid4().hex[:6]
    word = f"ibex{tag}"
    a, b = await source(f"prod-{tag}", "pay"), await source(f"stage-{tag}", "pay")
    await doc(a, "a.md", f"# {word}\ntext")
    await doc(b, "b.md", f"# {word}\ntext")
    assert [x["citation"]["path"] for x in (await find(person(("viewer", "prod-*", "p?y")), word))["results"]] == ["a.md"]
    assert len((await find(person(("viewer", "*", "*")), word))["results"]) == 2


async def test_a_revoked_binding_stops_results_at_once():
    tag = uuid.uuid4().hex[:6]
    await doc(await source(f"r-{tag}", "x"), "a.md", f"# moose{tag}\ntext")
    p = person(("viewer", f"r-{tag}", "x"))
    assert (await find(p, f"moose{tag}"))["results"]
    assert not (await find(Principal(p.user_id, p.email, []), f"moose{tag}"))["results"]


async def test_asking_for_a_scope_you_cannot_read_returns_nothing_even_shared_documents():
    tag = uuid.uuid4().hex[:6]
    await doc(await source(shared_globally=True), "shared.md", f"# otter{tag}\ntext")
    p = person(("viewer", f"mine-{tag}", "a"))
    assert (await find(p, f"otter{tag}"))["results"]
    r = await find(p, f"otter{tag}", environment=f"other-{tag}")
    assert r["results"] == [] and r["mode"] == "none"
    assert not (await find(p, f"otter{tag}", environment=f"mine-{tag}", namespace="b"))["results"]


async def test_request_scope_narrows_a_wide_reader():
    tag = uuid.uuid4().hex[:6]
    await doc(await source(f"a-{tag}", "x"), "a.md", f"# yak{tag}\ntext")
    await doc(await source(f"b-{tag}", "x"), "b.md", f"# yak{tag}\ntext")
    r = await find(person(("viewer", "*", "*")), f"yak{tag}", environment=f"a-{tag}")
    assert [x["citation"]["path"] for x in r["results"]] == ["a.md"]


def test_filters_preserve_pairs_for_every_backend():
    g = [access.Grant("prod", "team-a"), access.Grant("dev", "team-b")]
    os_filter, exact = access.opensearch(g)
    assert exact and len(os_filter["bool"]["should"]) == 2
    assert os_filter["bool"]["should"][0]["bool"]["filter"] == [{"terms": {"environment": ["*", "prod"]}}, {"terms": {"namespace": ["*", "team-a"]}}]
    pc, exact = access.pinecone(g)
    assert exact and pc == {"$or": [{"$and": [{"environment": {"$in": ["*", "prod"]}}, {"namespace": {"$in": ["*", "team-a"]}}]},
                                    {"$and": [{"environment": {"$in": ["*", "dev"]}}, {"namespace": {"$in": ["*", "team-b"]}}]}]}
    assert access.pinecone([access.Grant("prod-*", "a")])[1] is False  # says so, and search widens
    assert access.opensearch([access.Grant("*", "*")]) == (None, True)
    assert access.pinecone([]) == ({"environment": {"$in": []}}, True)


async def test_inexact_filters_expand_within_a_bound_and_the_final_check_decides(monkeypatch):
    tag = uuid.uuid4().hex[:6]
    env = f"prod-{tag}"
    good = await doc(await source(env, "a"), "good.md", "# Good\nthe readable one")
    bad = await doc(await source(env, "b"), "bad.md", "# Bad\nthe unreadable one")
    async with session_scope() as db:
        readable = (await db.execute(select(Chunk.id).where(Chunk.document_id == good))).scalar_one()
        hidden = (await db.execute(select(Chunk.id).where(Chunk.document_id == bad))).scalar_one()
    pools = []

    class Index:
        async def vector(self, name, qv, found, k):
            pools.append(k)
            return ([str(hidden)] * 0 + [str(uuid.uuid4()) for _ in range(k - 1)] + [str(hidden if k < 100 else readable)]), False

    monkeypatch.setattr(service, "external", lambda name=None: Index())
    monkeypatch.setattr(service, "_query_vector", lambda *a: _vector())

    async def _kw(self, *a):
        return []
    from moks.rag.backends import PostgresBackend
    monkeypatch.setattr(PostgresBackend, "keyword", _kw)
    async with session_scope() as db:
        idx = EmbeddingIndex(backend="eval", model_key="x", index_name="n", status="retired")
        db.add(idx)
    monkeypatch.setattr(service, "active_index", lambda db: _idx(idx))
    r = await find(person(("viewer", "prod-*", "a")), "readable one")
    assert [x["citation"]["path"] for x in r["results"]] == ["good.md"]
    assert r["candidates"]["expanded"] >= 1 and r["candidates"]["filtered_before_ranking"] is False
    assert len(pools) <= 4 and max(pools) <= service.MAX_POOL


async def _vector():
    return [0.1], None


async def _idx(idx):
    return idx


async def test_kb_search_stays_in_the_investigations_scope():
    from moks.scope import Scope
    from moks.tools.contracts import ToolContext
    from moks.tools.phase2 import KbArgs, kb_search

    tag = uuid.uuid4().hex[:6]
    word = f"heron{tag}"
    await doc(await source(f"dev-{tag}", "*"), "dev.md", f"# {word}\ndev")
    await doc(await source(f"prod-{tag}", "*"), "prod.md", f"# {word}\nprod")

    class Env:
        name = f"dev-{tag}"
    now = datetime.now(timezone.utc)
    ctx = ToolContext(None, Env, Scope(f"dev-{tag}", ("*",), ("*",), ()), person(("investigator", "*", "*")), now, now, {}, timedelta(hours=1))
    r = await kb_search(ctx, KbArgs(query=word))
    assert [h["citation"]["path"] for h in r.data] == ["dev.md"]
    assert "Documented knowledge only" in r.summary


# ---- governance --------------------------------------------------------------------------------


async def admin(client):
    email = f"a-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(email, ["admin"])
    return await login(client, email)


async def test_a_new_source_is_not_global_by_accident(client):
    h = await admin(client)
    tag = uuid.uuid4().hex[:6]
    r = await client.post("/moks/api/knowledge/sources", json={"name": f"s-{tag}", "type": "upload"}, headers=h)
    assert r.status_code == 422 and "readable by everyone" in r.text
    r = await client.post("/moks/api/knowledge/sources", json={"name": f"s-{tag}", "type": "upload", "shared_globally": True, "classification": "restricted"}, headers=h)
    assert r.status_code == 422 and "cannot be shared globally" in r.text
    r = await client.post("/moks/api/knowledge/sources", json={"name": f"s-{tag}", "type": "upload", "scope_environment": "prod", "team": "sre", "review_interval_days": 90}, headers=h)
    assert r.status_code == 201 and r.json()["team"] == "sre" and r.json()["review_overdue"] is True


async def test_an_upload_inherits_its_sources_scope_and_cannot_widen_it(client):
    h = await admin(client)
    tag = uuid.uuid4().hex[:6]
    sid = await source(f"up-{tag}", "shop")
    r = await client.post(f"/moks/api/knowledge/sources/{sid}/upload", files={"file": ("a.md", b"# A\ntext")}, headers=h)
    assert r.status_code == 201
    async with session_scope() as db:
        d = await db.get(Document, uuid.UUID(r.json()["id"]))
    assert (d.environment, d.namespace) == (f"up-{tag}", "shop")
    r = await client.post(f"/moks/api/knowledge/sources/{sid}/upload", files={"file": ("b.md", b"# B")}, data={"environment": "*"}, headers=h)
    assert r.status_code == 422


async def test_quarantine_stops_retrieval_keeps_documents_and_is_audited(client):
    h = await admin(client)
    tag = uuid.uuid4().hex[:6]
    sid = await source(f"q-{tag}", "x")
    await doc(sid, "a.md", f"# lynx{tag}\ntext")
    p = person(("viewer", f"q-{tag}", "x"))
    assert (await find(p, f"lynx{tag}"))["results"]
    r = await client.put(f"/moks/api/knowledge/sources/{sid}/lifecycle", json={"lifecycle": "quarantined", "reason": "bad content"}, headers=h)
    assert r.status_code == 200
    assert not (await find(p, f"lynx{tag}"))["results"]
    await client.put(f"/moks/api/knowledge/sources/{sid}/lifecycle", json={"lifecycle": "active", "reason": "reviewed"}, headers=h)
    assert (await find(p, f"lynx{tag}"))["results"]


async def test_stale_policy_flags_or_excludes():
    tag = uuid.uuid4().hex[:6]
    env = f"st-{tag}"
    flagged = await source(env, "a", review_interval_days=30, last_reviewed_at=datetime.now(timezone.utc) - timedelta(days=60))
    await doc(flagged, "a.md", f"# bat{tag}\nold content")
    p = person(("viewer", env, "*"))
    hit = (await find(p, f"bat{tag}"))["results"][0]
    assert hit["freshness"]["state"] == "stale" and "review is overdue" in hit["freshness"]["reason"]
    async with session_scope() as db:
        (await db.get(KnowledgeSource, flagged)).stale_policy = "exclude"
    assert not (await find(p, f"bat{tag}"))["results"]


async def test_access_preview_and_document_preview(client):
    h = await admin(client)
    tag = uuid.uuid4().hex[:6]
    env = f"ap-{tag}"
    sid = await source(env, "x")
    did = await doc(sid, "a.md", f"# fox{tag}\nbody")
    await make_user(f"u-{tag}@example.com", [f"viewer:{env}:x"])
    await make_user(f"v-{tag}@example.com", [f"viewer:{env}:y"])
    assert (await client.get(f"/moks/api/knowledge/sources/{sid}/access", params={"email": f"u-{tag}@example.com"}, headers=h)).json()["can_read"] is True
    no = (await client.get(f"/moks/api/knowledge/sources/{sid}/access", params={"email": f"v-{tag}@example.com"}, headers=h)).json()
    assert no["can_read"] is False and no["reason"] == "no binding covers this scope"
    preview = (await client.get(f"/moks/api/knowledge/documents/{did}", headers=h)).json()
    assert preview["chunks"][0]["lines"] == [1, 2]
    other = await login(client, f"v-{tag}@example.com")
    assert (await client.get(f"/moks/api/knowledge/documents/{did}", headers=other)).status_code == 404


async def test_rejected_content_is_never_stored_or_shown(client):
    h = await admin(client)
    sid = await source(f"sec-{uuid.uuid4().hex[:6]}", "x")
    did = await doc(sid, "key.md", "-----BEGIN RSA PRIVATE KEY-----\nabc123secretvalue\n-----END RSA PRIVATE KEY-----")
    d = (await client.get(f"/moks/api/knowledge/documents/{did}", headers=h)).json()
    assert d["status"] == "excluded" and d["chunks"] == [] and "private key" in d["excluded_reason"] and "abc123secretvalue" not in str(d)


async def test_deleting_a_source_keeps_a_tombstone_and_frees_the_name(client):
    h = await admin(client)
    tag = uuid.uuid4().hex[:6]
    body = {"name": f"del-{tag}", "type": "upload", "scope_environment": "prod"}
    sid = (await client.post("/moks/api/knowledge/sources", json=body, headers=h)).json()["id"]
    assert (await client.delete(f"/moks/api/knowledge/sources/{sid}", headers=h)).status_code == 200
    assert (await client.post("/moks/api/knowledge/sources", json=body, headers=h)).status_code == 201
    async with session_scope() as db:
        assert (await db.get(KnowledgeSource, uuid.UUID(sid))).lifecycle == "deleted"


# ---- chunking and citations --------------------------------------------------------------------


def test_a_long_line_is_kept_whole_across_chunks_with_exact_offsets():
    line = " ".join(f"value{i}=abcdefghijklmnopqrstuvwxyz" for i in range(2000))
    text_ = f"# Title\n\n{line}\nlast line\n"
    pieces = chunking.chunk("doc.md", text_)
    joined = "".join(text_[p.start_offset:p.end_offset] for p in pieces)
    assert "value1999=abcdefghijklmnopqrstuvwxyz" in joined and "last line" in joined
    for p in pieces:
        assert text_[p.start_offset:p.end_offset] == p.text
        assert p.tokens <= chunking.limits()[0] / chunking.CHARS_PER_TOKEN + 1
    assert {p.start_line for p in pieces if "value1000=" in p.text} == {3}
    assert max(p.end_offset for p in pieces) >= len(text_) - 1
    assert not any(" " not in p.text and len(p.text) > 2000 for p in pieces)


def test_an_unbroken_line_is_split_and_nothing_is_lost():
    blob = "x" * 10000
    pieces = chunking.chunk("data.txt", blob)
    covered = sorted((p.start_offset, p.end_offset) for p in pieces)
    assert covered[0][0] == 0 and covered[-1][1] == 10000 and all(a[1] >= b[0] for a, b in zip(covered, covered[1:]))


def test_markdown_keeps_heading_context_and_a_fence_together():
    fence = "```yaml\n" + "\n".join(f"key{i}: value" for i in range(30)) + "\n```\n"
    text_ = "# Guide\n\n## Deploy\n\n" + "intro line\n" * 80 + fence + "after\n"
    pieces = chunking.chunk("g.md", text_)
    holder = [p for p in pieces if "```yaml" in p.text]
    assert len(holder) == 1 and "```" in holder[0].text[8:] and holder[0].heading == "Guide > Deploy"


def test_a_heading_inside_a_fence_is_not_a_heading():
    pieces = chunking.chunk("g.md", "# Real\n\n```sh\n# just a comment\necho hi\n```\n")
    assert {p.heading for p in pieces} == {"Real"}


def test_yaml_uses_documents_and_names_the_resource():
    pieces = chunking.chunk("a.yaml", "kind: Deployment\nmetadata:\n  name: web\n---\nkind: Service\nmetadata:\n  name: api\n")
    assert [p.heading for p in pieces] == ["Deployment web", "Service api"]


async def test_chunk_identity_is_versioned_and_citations_separate_check_from_change():
    sid = await source(f"c-{uuid.uuid4().hex[:6]}", "x")
    a = await doc(sid, "a.md", "# A\nsame text", revision="rev1")
    async with session_scope() as db:
        first = (await db.execute(select(Chunk.id).where(Chunk.document_id == a))).scalars().all()
    assert first == [service.chunk_id(a, (await _hash(a)), 0)]
    await doc(sid, "a.md", "# A\nsame text", revision="rev2")
    async with session_scope() as db:
        d = await db.get(Document, a)
    assert d.changed_revision == "rev1" and d.revision == "rev2" and d.checked_at is not None
    await doc(sid, "a.md", "# A\nnew text", revision="rev3")
    async with session_scope() as db:
        d = await db.get(Document, a)
        second = (await db.execute(select(Chunk.id).where(Chunk.document_id == a))).scalars().all()
    assert d.changed_revision == "rev3" and first != second


async def _hash(did):
    async with session_scope() as db:
        return (await db.get(Document, did)).content_hash


async def test_an_oversized_document_is_excluded_with_a_reason(monkeypatch):
    monkeypatch.setattr(service, "MAX_CHUNKS", 1)
    sid = await source(f"o-{uuid.uuid4().hex[:6]}", "x")
    did = await doc(sid, "big.md", "word " * 5000)
    async with session_scope() as db:
        d = await db.get(Document, did)
    assert d.status == "excluded" and "limit" in d.excluded_reason


# ---- ingestion ---------------------------------------------------------------------------------


@pytest.fixture
def github(monkeypatch):
    holder = {}
    real = gitmod.GitHubProvider.__init__

    def init(self, config, cred, transport=None):
        real(self, config, cred, transport=httpx.MockTransport(holder["gh"]))
    monkeypatch.setattr(gitmod.GitHubProvider, "__init__", init)
    monkeypatch.setattr(ingest, "BLOB_DELAY_SECONDS", 0)
    return holder


async def run_sync(sid):
    async with session_scope() as db:
        src = await db.get(KnowledgeSource, sid)
        return await ingest.sync_github(db, src)


async def gh_source():
    return await source(f"gh-{uuid.uuid4().hex[:6]}", "x", type_="github", config={"repository": "acme/ops"})


async def test_a_large_repository_finishes_over_several_passes_at_one_revision(github, monkeypatch):
    monkeypatch.setattr(ingest, "BATCH_FILES", 3)
    gh = github["gh"] = FakeGitHub({f"docs/{i:02}.md": f"# {i}" for i in range(8)})
    sid = await gh_source()
    r = await run_sync(sid)
    assert (r["processed"], r["remaining"], r["content"], r["complete"]) == (3, 5, "in_progress", False)
    gh.files["docs/99.md"] = "# arrived later"  # a new commit must not disturb the pinned listing
    gh.rev = 2
    r = await run_sync(sid)
    assert r["revision"] == f"{1:040d}" and r["processed"] == 6
    r = await run_sync(sid)
    assert r["remaining"] == 0 and r["complete"] is True and r["discovered"] == 8
    r = await run_sync(sid)  # a fresh listing picks up the new commit
    assert r["revision"] == f"{2:040d}" and r["added"] == 1


async def test_a_file_that_keeps_failing_does_not_starve_later_ones(github, monkeypatch):
    monkeypatch.setattr(ingest, "BATCH_FILES", 2)

    class Flaky(FakeGitHub):
        def __call__(self, req):
            if "/git/blobs/" in req.url.path and req.url.path.endswith(self.sha("docs/00.md")):
                return httpx.Response(404)
            return super().__call__(req)
    github["gh"] = Flaky({f"docs/{i:02}.md": f"# {i}" for i in range(5)})
    sid = await gh_source()
    results = [await run_sync(sid) for _ in range(3)]
    last = results[-1]
    assert last["failed"] == 1 and last["processed"] == 4 and last["content"] == "partial" and last["remaining"] == 0
    async with session_scope() as db:
        item = await db.get(KnowledgeSyncItem, (sid, "docs/00.md"))
    assert item.state == "failed" and item.last_error


async def test_a_truncated_tree_is_walked_and_complete(github):
    class Big(FakeGitHub):
        def __call__(self, req):
            p = req.url.path
            if "/git/trees/" in p and req.url.params.get("recursive"):
                return httpx.Response(200, json={"truncated": True, "tree": []})
            if p.endswith(f"/git/trees/{1:040d}"):
                return httpx.Response(200, json={"tree": [{"path": "docs", "type": "tree", "sha": "t1"}, {"path": "top.md", "type": "blob", "sha": self.sha("top.md"), "size": 3}]})
            if p.endswith("/git/trees/t1"):
                return httpx.Response(200, json={"tree": [{"path": "a.md", "type": "blob", "sha": self.sha("docs/a.md"), "size": 3}]})
            return super().__call__(req)
    github["gh"] = Big({"top.md": "# t", "docs/a.md": "# a"})
    r = await run_sync(await gh_source())
    assert r["listing"] == "complete" and r["discovered"] == 2 and r["complete"]


async def test_a_walk_past_its_bound_is_partial_and_deletes_nothing(github, monkeypatch):
    gh = github["gh"] = FakeGitHub({"docs/a.md": "# a", "docs/b.md": "# b"})
    sid = await gh_source()
    await run_sync(sid)
    real = gitmod.GitHubProvider.inventory

    async def partial(self, repo, ref):
        sha, files, _ = await real(self, repo, ref)
        return sha, files[:1], "partial"
    monkeypatch.setattr(gitmod.GitHubProvider, "inventory", partial)
    r = await run_sync(sid)
    assert r["listing"] == "partial" and r["deleted"] == 0 and r["deletions_skipped"] and r["complete"] is False
    assert gh is github["gh"]


async def test_a_failed_listing_is_recorded(github):
    class Down(FakeGitHub):
        def __call__(self, req):
            return httpx.Response(503) if "/git/trees/" in req.url.path else super().__call__(req)
    github["gh"] = Down({"a.md": "# a"})
    sid = await gh_source()
    with pytest.raises(ConnectorError):
        await run_sync(sid)
    async with session_scope() as db:
        assert (await db.get(KnowledgeSource, sid)).sync_cursor["listing"] == "failed"


async def test_a_pipeline_change_reprocesses_unchanged_content(github, monkeypatch):
    gh = github["gh"] = FakeGitHub({"docs/a.md": "# a"})
    sid = await gh_source()
    await run_sync(sid)
    reads = gh.blob_reads
    await run_sync(sid)
    assert gh.blob_reads == reads
    monkeypatch.setattr(ingest, "PIPELINE_VERSION", 99)
    monkeypatch.setattr(service, "PIPELINE_VERSION", 99)
    r = await run_sync(sid)
    assert gh.blob_reads == reads + 1 and r["complete"]
    async with session_scope() as db:
        assert (await db.execute(select(Document.pipeline_version).where(Document.source_id == sid))).scalar_one() == 99


async def test_a_file_too_large_is_excluded_and_its_old_text_stops_being_searchable(github, monkeypatch):
    tag = uuid.uuid4().hex[:6]
    gh = github["gh"] = FakeGitHub({"docs/a.md": f"# seal{tag}\nsmall"})
    sid = await gh_source()
    await run_sync(sid)
    assert (await find(person(("viewer", "*", "*")), f"seal{tag}"))["results"]
    monkeypatch.setattr(ingest, "MAX_FILE_BYTES", 4)
    gh.files["docs/a.md"] = "# much larger now"
    r = await run_sync(sid)
    assert r["excluded"] == 1 and r["skipped_too_large"] == ["docs/a.md"]
    assert not (await find(person(("viewer", "*", "*")), f"seal{tag}"))["results"]


# ---- index consistency -------------------------------------------------------------------------


class Index:
    def __init__(self):
        self.docs, self.fail, self.counts, self.during = {}, 0, {}, None

    async def ensure_index(self, name, dims):
        pass

    async def delete_document(self, name, document_id):
        if self.fail:
            self.fail -= 1
            raise ConnectorError("unavailable", "index down")
        self.docs.pop(str(document_id), None)

    async def index_chunks(self, name, rows, embeddings):
        if self.during:
            hook, self.during = self.during, None
            await hook()
        for r in rows:
            self.docs[r["document_id"]] = r["text"]

    async def count_document(self, name, document_id):
        return self.counts.get(str(document_id), 1 if str(document_id) in self.docs else 0)

    async def keyword(self, *a):
        return [], True

    async def drop_index(self, name):
        pass


@pytest.fixture
async def ext(monkeypatch):
    fake = Index()
    monkeypatch.setattr(service, "external", lambda name=None: fake)
    monkeypatch.setattr(service, "backend_name", lambda: "opensearch")
    async with session_scope() as db:
        await db.execute(text("UPDATE index_outbox SET status = 'done' WHERE status <> 'done'"))
        await db.execute(text("UPDATE embedding_indexes SET status = 'retired' WHERE backend = 'opensearch' AND status = 'active'"))
        await db.execute(text("UPDATE documents SET indexed_revision = content_revision"))
        db.add(EmbeddingIndex(backend="opensearch", model_key="keyword-only", index_name="active", status="active"))
    yield fake


async def _drain():
    return await service.drain_outbox(50)


async def test_an_update_during_a_write_is_not_overwritten(ext):
    sid = await source(f"u-{uuid.uuid4().hex[:6]}", "x")
    did = await doc(sid, "a.md", "# A\nfirst")

    async def newer():
        await doc(sid, "a.md", "# A\nsecond")
    ext.during = newer
    await _drain()
    await _drain()
    assert "second" in ext.docs[str(did)]
    async with session_scope() as db:
        d = await db.get(Document, did)
    assert d.indexed_revision == d.content_revision


async def test_one_document_is_written_by_one_worker_at_a_time(ext):
    sid = await source(f"l-{uuid.uuid4().hex[:6]}", "x")
    did = await doc(sid, "a.md", "# A\ntext")
    async with session_scope() as holder:
        assert await service._lock_document(holder, did)
        r = await _drain()
    assert r["deferred"] >= 1 and r["applied"] == 0
    async with session_scope() as db:
        await db.execute(text("UPDATE index_outbox SET next_attempt_at = now() WHERE document_id = :d"), {"d": did})
    assert (await _drain())["applied"] == 1


async def test_a_delete_is_idempotent_and_survives_a_failure(ext):
    sid = await source(f"d-{uuid.uuid4().hex[:6]}", "x")
    did = await doc(sid, "a.md", "# A\ntext")
    await _drain()
    async with session_scope() as db:
        await service.remove_document(db, await db.get(Document, did))
    ext.fail = 1
    assert (await _drain())["retry"] == 1
    async with session_scope() as db:
        await db.execute(text("UPDATE index_outbox SET next_attempt_at = now() WHERE document_id = :d AND status = 'pending'"), {"d": did})
        assert (await db.get(Document, did)).status == "deleted"  # the tombstone waits for the index
    await _drain()
    assert str(did) not in ext.docs


async def test_dead_letters_are_visible_and_retried_one_document_at_a_time(ext, monkeypatch):
    monkeypatch.setattr(service, "OUTBOX_MAX_ATTEMPTS", 1)
    sid = await source(f"x-{uuid.uuid4().hex[:6]}", "x")
    a, b = await doc(sid, "a.md", "# A"), await doc(sid, "b.md", "# B")
    ext.fail = 2
    await _drain()
    async with session_scope() as db:
        s = await service.stats(db)
    assert s["outbox"]["dead"] == 2
    assert await service.retry_dead(a) == 1
    await _drain()
    assert str(a) in ext.docs and str(b) not in ext.docs


async def test_an_index_that_lost_a_document_is_found_and_repaired(ext):
    sid = await source(f"v-{uuid.uuid4().hex[:6]}", "x")
    did = await doc(sid, "a.md", "# A\ntext")
    await _drain()
    ext.docs.pop(str(did))
    r = await service.verify_index(100000)
    assert r["mismatched"] >= 1
    await service.drain_outbox(100000)
    assert str(did) in ext.docs


async def test_a_document_indexed_without_vectors_is_visible_and_backfilled(monkeypatch):
    calls = {"fail": True}

    async def embed(db, idx, texts, tries=2):
        if calls["fail"]:
            raise LLMError("unavailable", "embedding provider down")
        return [[0.1, 0.2]] * len(texts)
    monkeypatch.setattr(service, "_embed", embed)
    written = []
    monkeypatch.setattr("moks.rag.backends.PostgresBackend.index_chunks", lambda self, idx, rows, emb: _record(written, emb))
    async with session_scope() as db:
        idx = await service.ensure_index(db)
        old = idx.dimensions
        idx.dimensions = 2
    try:
        sid = await source(f"e-{uuid.uuid4().hex[:6]}", "x")
        did = await doc(sid, "a.md", "# A\ntext")
        async with session_scope() as db:
            d = await db.get(Document, did)
            assert d.vector_state == "missing" and "embedding provider down" in d.vector_error
            s = await service.stats(db)
        assert s["embedding"]["missing"] >= 1 and s["embedding"]["coverage"] < 1
        calls["fail"] = False
        for _ in range(30):
            await service.backfill_vectors()
            async with session_scope() as db:
                if (await db.get(Document, did)).vector_state == "complete":
                    break
        async with session_scope() as db:
            assert (await db.get(Document, did)).vector_state == "complete"
    finally:
        async with session_scope() as db:
            (await db.execute(select(EmbeddingIndex).where(EmbeddingIndex.backend == "postgres", EmbeddingIndex.status == "active"))).scalar_one().dimensions = old


async def _record(written, emb):
    written.append(emb)


# ---- retrieval quality --------------------------------------------------------------------------


async def test_an_exact_identifier_outranks_prose_and_the_response_says_how_it_was_found():
    tag = uuid.uuid4().hex[:6]
    code = f"ERR_{tag.upper()}_4071"
    sid = await source(f"id-{tag}", "x")
    for i in range(8):
        await doc(sid, f"n{i}.md", f"# Error handling notes {i}\nerror error error handling code retry {tag}")
    await doc(sid, "exact.md", f"# Ledger\nThe ledger answers {code} when the key repeats.")
    r = await find(person(("viewer", f"id-{tag}", "x")), code)
    assert r["results"][0]["citation"]["path"] == "exact.md" and r["results"][0]["signals"]["exact_identifier"] > 0
    assert r["evidence"]["state"] == "supported" and "not a probability" in r["score_note"]


async def test_duplicates_collapse_and_superseded_documents_are_marked_and_rank_lower():
    tag = uuid.uuid4().hex[:6]
    p = person(("viewer", f"q-{tag}", "x"))
    sid = await source(f"q-{tag}", "x")
    same = f"# Restart {tag}\nRestart the service with the standard procedure for {tag} and verify health."
    await doc(sid, "one.md", same)
    await doc(sid, "two.md", same)
    assert len((await find(p, f"restart {tag}"))["results"]) == 1
    await doc(sid, "old.md", f"---\nsuperseded_by: new.md\n---\n# Failover {tag}\nFailover with the old tool for {tag} now.")
    await doc(sid, "new.md", f"# Failover {tag}\nFailover with the current pipeline for {tag} and confirm.")
    r = (await find(p, f"failover {tag}"))["results"]
    old = next(x for x in r if x["citation"]["path"] == "old.md")
    assert r[0]["citation"]["path"] == "new.md" and old["freshness"]["state"] == "superseded" and "new.md" in old["freshness"]["reason"]
    assert r[0]["freshness"].get("possible_conflict") is True


async def test_neighbors_add_context_within_the_same_document():
    tag = uuid.uuid4().hex[:6]
    sid = await source(f"n-{tag}", "x")
    body = "# Guide\n\n" + "\n\n".join(f"paragraph {i} " + "filler text " * 80 for i in range(4)) + f"\n\nneedle{tag} here"
    await doc(sid, "g.md", body)
    r = await find(person(("viewer", f"n-{tag}", "x")), f"needle{tag}", k=1, neighbors=True)
    assert r["results"][0]["context"]


async def test_no_answer_is_useful_rather_than_a_guess():
    r = await find(person(("viewer", "nowhere", "x")), "completely unknown thing")
    assert r["results"] == [] and r["evidence"]["state"] == "none" and "Nothing here says" in r["evidence"]["reason"]


async def test_embedding_outage_falls_back_and_says_why(monkeypatch):
    async def down(*a, **k):
        raise LLMError("unavailable", "provider down")
    tag = uuid.uuid4().hex[:6]
    await doc(await source(f"f-{tag}", "x"), "a.md", f"# puffin{tag}\ntext")

    class Cfg:
        id, provider, model, embedding_dimensions = uuid.uuid4(), "p", "m", 2

    async def cfg(db):
        return Cfg

    class Idx:
        id, dimensions, index_name, llm_config_id, model_key = uuid.uuid4(), 2, "i", Cfg.id, service.model_key(Cfg)

    async def idx(db):
        return Idx

    async def has(self):
        return True
    monkeypatch.setattr(service, "embedding_config", cfg)
    monkeypatch.setattr(service, "active_index", idx)
    monkeypatch.setattr("moks.rag.backends.PostgresBackend.has_vector", has)
    monkeypatch.setattr(service, "provider_for", lambda c: None)
    monkeypatch.setattr(service.budget, "embed", down)
    r = await find(person(("viewer", f"f-{tag}", "x")), f"puffin{tag}")
    assert r["mode"] == "keyword" and "embedding failed" in r["fallback_reason"] and r["results"]
