"""The Knowledge Explorer, retrieval workspace and feedback, with the corrections that came with them.

Every document here is real rows in PostgreSQL. No test reaches OpenSearch: the Explorer never does."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from moks import jobs
from moks.db import session_scope
from moks.llm.providers import LLMError
from moks.models import AuditEvent, Chunk, Document, DocumentIndexState, IndexOutbox, KnowledgeSource, RetrievalFeedback
from moks.rag import chunking, explore, ingest, secrets, service, web
from moks.rbac import ROLES, Binding, P, Principal
from moks.redaction import redact_lines
from tests.conftest import login, make_user

API = "/moks/api/knowledge"


def tag() -> str:
    return uuid.uuid4().hex[:8]


async def source(env, ns, **kw):
    async with session_scope() as db:
        s = KnowledgeSource(name=kw.pop("name", f"x-{tag()}"), type=kw.pop("type", "upload"), scope_environment=env, scope_namespace=ns, config=kw.pop("config", {}), **kw)
        db.add(s)
        await db.flush()
        return s.id


async def doc(sid, path, body, **extra):
    url = extra.pop("url", None)
    async with session_scope() as db:
        return (await service.index_document(db, await db.get(KnowledgeSource, sid), path, body, revision=extra.pop("revision", None), url=url, extra=extra)).id


def person(*bindings):
    return Principal(str(uuid.uuid4()), "p@x", [Binding(*b) for b in bindings])


@pytest.fixture
async def world():
    """prod/team-a and dev/team-b are one person's bindings: prod/team-b must stay out of reach."""
    t = tag()
    prod, dev = f"prod-{t}", f"dev-{t}"
    a, b, c = await source(prod, "team-a", owner="sre", team="payments"), await source(prod, "team-b", name=f"secret-source-{t}"), await source(dev, "team-b")
    ok = await doc(a, "runbooks/a.md", f"# Failover {t}\nPromote the replica for {t}.", service="redis")
    hidden = await doc(b, "runbooks/b.md", f"# Hidden {t}\nThe private {t} procedure.", service="ledger")
    dev_doc = await doc(c, "runbooks/c.md", f"# Dev {t}\nFlush the {t} cache.")
    email = f"reader-{t}@example.com"
    await make_user(email, [f"viewer:{prod}:team-a", f"viewer:{dev}:team-b"])
    return {"t": t, "prod": prod, "dev": dev, "a": a, "b": b, "ok": ok, "hidden": hidden, "dev_doc": dev_doc, "email": email}


async def get(client, email, path):
    await login(client, email)
    return await client.get(f"{API}{path}")


# ---- direct objects, counts and filter discovery ---------------------------------------------------


async def test_a_person_sees_only_whole_bindings(client, world):
    r = (await get(client, world["email"], f"/explorer/documents?q={world['t']}")).json()
    assert {d["id"] for d in r["documents"]} == {str(world["ok"]), str(world["dev_doc"])}
    assert r["total"] == 2


async def test_counts_and_filter_options_hide_what_cannot_be_read(client, world):
    r = (await get(client, world["email"], "/explorer/documents?size=100")).json()
    seen = str(r)
    assert f"secret-source-{world['t']}" not in seen and "ledger" not in r["facets"]["service"]
    assert world["prod"] in r["facets"]["environment"] and "team-b" in r["facets"]["namespace"]
    assert sum(r["facets"]["environment"].values()) == r["total"]
    names = {p["name"] for p in r["progress"]}
    assert f"secret-source-{world['t']}" not in names


async def test_filtering_on_an_unreadable_value_finds_nothing(client, world):
    r = (await get(client, world["email"], f"/explorer/documents?source=secret-source-{world['t']}&service=ledger")).json()
    assert r["total"] == 0 and r["documents"] == []


async def test_direct_links_answer_like_a_missing_document(client, world):
    for path in (f"/explorer/documents/{world['hidden']}", f"/explorer/documents/{world['hidden']}/chunks", f"/documents/{world['hidden']}"):
        hidden, missing = await get(client, world["email"], path), await get(client, world["email"], path.replace(str(world["hidden"]), str(uuid.uuid4())))
        assert hidden.status_code == missing.status_code == 404 and hidden.json() == missing.json()


async def test_status_shows_a_reader_only_their_own_totals(client, world):
    r = (await get(client, world["email"], "/status")).json()
    assert r["scoped"] is True and r["backend"] is None and "outbox" not in r and "incomplete_sample" not in str(r)
    assert r["documents"]["active"] >= 2  # plus anything other tests shared globally
    admin = f"a-{world['t']}@example.com"
    await make_user(admin, ["admin"])
    assert "outbox" in (await get(client, admin, "/status")).json()


async def test_search_keeps_index_details_from_readers(client, world):
    r = (await get(client, world["email"], f"/search?q={world['t']}")).json()
    assert r["results"] and "index" not in r and "candidates" not in r


async def test_quarantined_and_excluded_documents_are_for_reviewers(client, world):
    t = world["t"]
    excluded = await doc(world["a"], ".env", "TOKEN=abc")
    async with session_scope() as db:
        (await db.get(Document, world["ok"])).lifecycle = "quarantined"
    assert (await get(client, world["email"], f"/explorer/documents/{world['ok']}")).status_code == 404
    assert (await get(client, world["email"], f"/explorer/documents/{excluded}")).status_code == 404
    assert (await get(client, world["email"], f"/documents/{world['ok']}")).status_code == 404
    reviewer = f"rev-{t}@example.com"
    await make_user(reviewer, [f"knowledge_reviewer:{world['prod']}:team-a"])
    r = await get(client, reviewer, f"/explorer/documents/{world['ok']}")
    assert r.status_code == 200 and r.json()["lifecycle"] == "quarantined" and r.json()["can_review"] is True
    assert (await get(client, reviewer, f"/explorer/documents/{excluded}")).json()["excluded_reason"]
    assert (await get(client, reviewer, f"/explorer/documents/{world['hidden']}")).status_code == 404


async def test_a_reviewer_cannot_act_outside_their_binding(client, world):
    reviewer = f"rev-{world['t']}@example.com"
    await make_user(reviewer, [f"knowledge_reviewer:{world['prod']}:team-a"])
    headers = await login(client, reviewer)
    assert (await client.post(f"{API}/documents/{world['hidden']}/review", json={}, headers=headers)).status_code == 404
    assert (await client.put(f"{API}/documents/{world['hidden']}/lifecycle", json={"state": "quarantined"}, headers=headers)).status_code == 404
    assert (await client.post(f"{API}/documents/{world['ok']}/review", json={}, headers=headers)).status_code == 200
    assert (await client.get(f"{API}/sources")).status_code == 403  # reviewing is not administering sources


async def test_permissions_are_separate_jobs():
    assert P.KNOWLEDGE_READ in ROLES["viewer"] and P.KNOWLEDGE_REVIEW not in ROLES["operator"] and P.KNOWLEDGE_INDEX not in ROLES["knowledge_admin"]
    assert P.KNOWLEDGE_SOURCE_ADMIN not in ROLES["knowledge_reviewer"] and P.KNOWLEDGE_SOURCE_ADMIN not in ROLES["index_operator"] and P.SKILL_PUBLISH not in ROLES["knowledge_admin"]
    scoped = person(("knowledge_admin", "prod", "*"))
    assert not scoped.can(P.KNOWLEDGE_SOURCE_ADMIN)  # a source can reach everyone, so this needs a global binding


# ---- document details -------------------------------------------------------------------------------


async def test_detail_never_carries_vectors_and_redacts_the_preview(client, world):
    s = await source(world["prod"], "team-a", team="sre")
    d = await doc(s, "runbooks/d.md", f"# Detail {world['t']}\npassword: hunter22-secret\n" + "\n\n".join(f"Paragraph {i} about {world['t']}." * 8 for i in range(40)), url="javascript:alert(1)")
    r = (await get(client, world["email"], f"/explorer/documents/{d}")).json()
    assert "hunter22" not in r["preview"] and "embedding" in r and "vector" not in r["embedding"]
    assert r["url"] is None and r["citation"]["url"] is None  # a stored value never becomes a script link
    page = (await get(client, world["email"], f"/explorer/documents/{d}/chunks?limit=2")).json()
    assert len(page["chunks"]) == 2 and page["total"] > 2 and page["chunks"][0]["lines"][0] >= 1 and page["chunks"][0]["offsets"][1] > 0


async def test_preview_rejoins_overlapping_chunks_without_repeating_them():
    body = "\n".join(f"line {i} of the document with enough words to fill a chunk" for i in range(120))
    pieces = chunking.chunk("a.txt", body)
    assert len(pieces) > 2 and pieces[1].start_offset < pieces[0].end_offset  # overlap exists
    rows = [Chunk(text=p.text, start_offset=p.start_offset, end_offset=p.end_offset) for p in pieces]
    assert explore._preview(rows)[0] == body


async def test_only_index_operators_see_retry_detail(client, world):
    reader = (await get(client, world["email"], f"/explorer/documents/{world['ok']}")).json()
    assert "operations" not in reader and reader["generations"] == [] or all(g["index"] is None for g in reader["generations"])
    op = f"op-{world['t']}@example.com"
    await make_user(op, [f"viewer:{world['prod']}:team-a", "index_operator"])
    r = (await get(client, op, f"/explorer/documents/{world['ok']}")).json()
    assert "operations" in r and r["can_operate"] is True


async def test_replacement_links_stay_within_what_can_be_read(client, world):
    newer = await doc(world["a"], "runbooks/new.md", f"# New {world['t']}\nThe current way.")
    admin = f"admin-{world['t']}@example.com"
    await make_user(admin, ["admin"])
    headers = await login(client, admin)
    r = await client.put(f"{API}/documents/{world['ok']}/lifecycle", json={"state": "active", "superseded_by": str(newer)}, headers=headers)
    assert r.status_code == 200
    d = (await client.get(f"{API}/explorer/documents/{world['ok']}")).json()
    assert d["superseded_by"]["id"] == str(newer)
    assert (await client.get(f"{API}/explorer/documents/{newer}")).json()["replaces"][0]["id"] == str(world["ok"])
    wider = await client.put(f"{API}/documents/{world['dev_doc']}/lifecycle", json={"state": "active", "superseded_by": str(newer)}, headers=headers)
    assert wider.status_code == 409  # readers of the dev document could not read the replacement


async def test_review_sets_the_next_date_from_the_source_interval(client, world):
    s = await source(world["prod"], "team-a", review_interval_days=30)
    d = await doc(s, "runbooks/r.md", "# Review\nSomething to review carefully.")
    admin = f"adm-{world['t']}@example.com"
    await make_user(admin, ["admin"])
    r = await client.post(f"{API}/documents/{d}/review", json={}, headers=await login(client, admin))
    assert datetime.fromisoformat(r.json()["review_due_at"]) > datetime.now(timezone.utc) + timedelta(days=29)


async def test_freshness_state_in_the_list_matches_what_search_reports(world):
    s = await source(world["prod"], "team-a", stale_after_days=1)
    d = await doc(s, "runbooks/old.md", f"# Old {world['t']}\nOld guidance on {world['t']}.")
    async with session_scope() as db:
        await db.execute(text("UPDATE documents SET checked_at = now() - interval '10 days' WHERE id = :d"), {"d": d})
    p = person(("viewer", world["prod"], "team-a"))
    async with session_scope() as db:
        found = await explore.browse(db, p, world["t"], {"freshness": {"stale"}})
        hit = await service.search(db, p, f"Old {world['t']}", 5)
    assert [x["id"] for x in found["documents"]] == [str(d)]
    assert next(r for r in hit["results"] if r["document_id"] == str(d))["freshness"]["state"] == "stale"


# ---- ingestion and index corrections ----------------------------------------------------------------


async def test_an_empty_document_does_not_hold_up_backfill_or_fail_a_reindex():
    s = await source(f"e-{tag()}", "x")
    empty = await doc(s, "empty.md", "   \n")
    async with session_scope() as db:
        d = await db.get(Document, empty)
        vectors = type("I", (), {"dimensions": 8})()
        service._note_vectors(d, vectors, 0, False, None)
        assert d.vector_state == "not_applicable"
        assert service._gap(None, d.content_revision, vectors, 0) is None


async def test_a_document_that_keeps_failing_steps_back_in_backfill(monkeypatch):
    s = await source(f"e-{tag()}", "x")
    first, second = await doc(s, "a.md", "# A\nfirst document text"), await doc(s, "b.md", "# B\nsecond document text")
    seen = []

    async def fake_postgres(db, idx, d, src, chunks):
        seen.append(d.id)
        d.vector_state = "missing" if d.id == first else "complete"

    monkeypatch.setattr(service, "_write_postgres", fake_postgres)
    monkeypatch.setattr(service, "active_index", lambda db: _index(dims=8))
    async with session_scope() as db:
        await db.execute(text("UPDATE documents SET vector_state = 'not_applicable' WHERE id NOT IN (:a, :b) AND vector_state IN ('missing', 'unknown')"), {"a": first, "b": second})
        await db.execute(text("UPDATE documents SET vector_state = 'missing', updated_at = now() - interval '1 hour' WHERE id = :a"), {"a": first})
        await db.execute(text("UPDATE documents SET vector_state = 'missing' WHERE id = :b"), {"b": second})
    assert (await service.backfill_vectors())["failed"] == 1
    assert (await service.backfill_vectors())["filled"] == 1
    assert seen[:2] == [first, second]


async def _index(dims):
    return type("I", (), {"dimensions": dims, "activation": "complete", "id": uuid.uuid4()})()


def test_blank_lines_before_a_chunk_do_not_move_its_citation():
    body = "# Title\n\n\n\nThe first real line\nThe second real line\n"
    piece = chunking.chunk("a.md", body)[0]
    assert body[piece.start_offset:piece.end_offset] == piece.text
    p2 = chunking.chunk("a.txt", "\n\n\nreal text here\n")[0]
    assert (p2.start_line, p2.end_line) == (4, 4)


def test_only_newlines_count_as_lines():
    body = "one\x0ctwo three\nfour\n"
    piece = chunking.chunk("a.txt", body)[0]
    assert (piece.start_line, piece.end_line) == (1, 2)


def test_redaction_keeps_line_numbers():
    body = 'password: "first\nsecond"\nnext line\n'
    assert redact_lines(body).count("\n") == body.count("\n") and "second" not in redact_lines(body) and redact_lines(body).endswith("next line\n")


def test_a_nul_anywhere_excludes_the_file():
    assert secrets.exclusion_reason("a.md", "x" * 5000 + "\x00") == "binary content"


def test_long_lines_lose_nothing():
    body = "word " * 4000
    pieces = chunking.chunk("a.txt", body)
    rebuilt = "".join(p.text + " " for p in pieces).split()
    assert len(pieces) > 1 and rebuilt == body.split()


def test_identifiers_stay_whole():
    found = service.identifiers("pod in CrashLoopBackOff and ImagePullBackOff after OOMKilled, see MOKS_RAG_BACKEND in deploy/helm/values.yaml")
    assert {"CrashLoopBackOff", "ImagePullBackOff", "OOMKilled", "MOKS_RAG_BACKEND", "deploy/helm/values.yaml"} <= set(found)


def test_line_links_only_exist_on_a_git_host():
    assert service._line_url("https://github.example.com/a/b/blob/abc/x.py", 3, 5, "github", "code").endswith("#L3-L5")
    assert service._line_url("https://github.com/a/b/blob/abc/x.md", 3, 5, "github", "markdown").endswith("?plain=1#L3-L5")
    assert service._line_url("https://docs.github.com/page", 3, 5, "web", "markdown") == "https://docs.github.com/page"
    assert service._line_url("javascript:alert(1)", 3, 5, "github", "code") is None


async def test_a_web_404_deletes_nothing_until_a_pass_is_complete(monkeypatch):
    s = await source(f"w-{tag()}", "x", type="web", config={"urls": ["https://e.test/a", "https://e.test/b"]})
    await doc(s, "https://e.test/a", "# A\nPage a")
    b = await doc(s, "https://e.test/b", "# B\nPage b")

    async def fetch(url, **kw):
        if url.endswith("/a"):
            raise web.PageGone()
        raise service.ConnectorError("unavailable", "site down")

    monkeypatch.setattr(web, "fetch", fetch)
    monkeypatch.setattr(ingest, "PAGE_DELAY_SECONDS", 0)
    async with session_scope() as db:
        out = await ingest.sync_web(db, await db.get(KnowledgeSource, s))
    assert out["complete"] is False
    async with session_scope() as db:
        assert {d.status for d in (await db.execute(select(Document).where(Document.source_id == s))).scalars()} == {"active"}
        assert (await db.get(Document, b)).status == "active"


async def test_a_page_cut_short_says_so(monkeypatch):
    s = await source(f"w-{tag()}", "x", type="web", config={"urls": ["https://e.test/long"]})

    async def fetch(url, **kw):
        return url, "text/plain", "words " * 80_000

    monkeypatch.setattr(web, "fetch", fetch)
    monkeypatch.setattr(ingest, "PAGE_DELAY_SECONDS", 0)
    async with session_scope() as db:
        await ingest.sync_web(db, await db.get(KnowledgeSource, s))
    async with session_scope() as db:
        d = (await db.execute(select(Document).where(Document.source_id == s))).scalar_one()
        assert "first 300 KB" in d.ingest_note


async def test_a_page_limit_is_not_a_complete_sync(monkeypatch):
    s = await source(f"w-{tag()}", "x", type="web", config={"urls": ["https://e.test/a", "https://e.test/b"], "max_pages": 1})

    async def fetch(url, **kw):
        return url, "text/plain", "some text"

    monkeypatch.setattr(web, "fetch", fetch)
    monkeypatch.setattr(ingest, "PAGE_DELAY_SECONDS", 0)
    async with session_scope() as db:
        assert (await ingest.sync_web(db, await db.get(KnowledgeSource, s)))["complete"] is False


async def test_an_unchanged_file_moves_its_link_with_its_revision():
    s = await source(f"g-{tag()}", "x")
    a = "https://github.com/a/b/blob/aaa/x.md"
    b = "https://github.com/a/b/blob/bbb/x.md"
    async with session_scope() as db:
        src = await db.get(KnowledgeSource, s)
        d = await service.index_document(db, src, "x.md", "# X\nbody", revision="aaa", url=a, content_hash="git:1")
        await service.index_document(db, src, "x.md", "# X\nbody", revision="bbb", url=b, content_hash="git:1")
        assert (d.revision, d.url) == ("bbb", b)


async def test_a_quarantined_source_is_not_scheduled_and_a_draft_one_is_but_not_searched():
    t = tag()
    s = await source(f"w-{t}", "x", type="github", lifecycle="draft", config={"repository": "a/b"}, sync_interval_minutes=60)
    q = await source(f"w-{t}", "x", type="github", lifecycle="quarantined", config={"repository": "a/c"}, sync_interval_minutes=60)
    assert "draft" in jobs.SYNCING and "quarantined" not in jobs.SYNCING
    d = await doc(s, "a.md", f"# Draft {t}\nDraft text {t}.")
    async with session_scope() as db:
        hit = await service.search(db, person(("viewer", "*", "*")), f"Draft {t}", 5)
    assert str(d) not in {r["document_id"] for r in hit["results"]} and q


# ---- activation --------------------------------------------------------------------------------------


async def test_reactivating_a_generation_reports_what_it_missed(monkeypatch):
    async with session_scope() as db:
        current = await service.ensure_index(db)
        before = current.index_name
    s = await source(f"a-{tag()}", "x")
    d = await doc(s, "a.md", "# A\nAdded while a generation was away.")
    async with session_scope() as db:
        old = (await db.execute(text("INSERT INTO embedding_indexes (id, backend, model_key, index_name, status, activation, mapping_version, cleanup_pending, chunk_count, retired_at) VALUES (gen_random_uuid(), :b, 'keyword-only', :n, 'retired', 'complete', 2, true, 0, now()) RETURNING index_name"), {"b": service.backend_name(), "n": f"old-{tag()}"})).scalar_one()
    async with session_scope() as db:
        r = await service.activate_generation(db, old)
    assert r["activation"] == "degraded" and r["incomplete_documents"] >= 1
    async with session_scope() as db:
        idx = await service.active_index(db)
        assert idx.activation == "degraded"
        await service.activate_generation(db, before)
        assert d
        await db.flush()
        await db.execute(text("DELETE FROM embedding_indexes WHERE index_name = :n"), {"n": old})


# ---- retrieval workspace ------------------------------------------------------------------------------


async def test_workspace_compares_modes_and_says_what_ran(client, world):
    tester = f"t-{world['t']}@example.com"
    await make_user(tester, [f"investigator:{world['prod']}:team-a"])
    h = await login(client, tester)
    r = await client.post(f"{API}/retrieval/test", json={"query": f"Failover {world['t']}", "modes": ["keyword", "vector", "hybrid"]}, headers=h)
    assert r.status_code == 200, r.text
    by = {v["name"]: v for v in r.json()["variants"]}
    assert by["keyword"]["mode"] == "keyword" and by["keyword"]["results"][0]["document_id"] == str(world["ok"])
    assert by["vector"]["mode"] == "none" and by["vector"]["results"] == [] and by["vector"]["fallback_reason"]  # no vectors here, and it says so
    assert by["hybrid"]["mode"] == "keyword"  # hybrid was asked for; keyword is what ran
    assert "diagnostics" not in by["keyword"] and r.json()["reranker"]["configured"] is False


async def test_workspace_cannot_widen_a_scope(client, world):
    tester = f"t2-{world['t']}@example.com"
    await make_user(tester, [f"investigator:{world['prod']}:team-a"])
    h = await login(client, tester)
    r = await client.post(f"{API}/retrieval/test", json={"query": f"private {world['t']} procedure", "environment": world["prod"], "namespace": "team-b", "sources": [str(world["b"])], "modes": ["keyword"]}, headers=h)
    assert r.json()["variants"][0]["results"] == []
    r = await client.post(f"{API}/retrieval/test", json={"query": f"private {world['t']} procedure", "sources": [str(world["b"])], "modes": ["keyword"]}, headers=h)
    assert r.json()["variants"][0]["results"] == []


async def test_workspace_needs_its_permission(client, world):
    h = await login(client, world["email"])  # viewer
    assert (await client.post(f"{API}/retrieval/test", json={"query": "anything"}, headers=h)).status_code == 403


async def test_diagnostics_go_to_index_operators(client, world):
    op = f"o2-{world['t']}@example.com"
    await make_user(op, [f"viewer:{world['prod']}:team-a", "index_operator"])
    h = await login(client, op)
    r = await client.post(f"{API}/retrieval/test", json={"query": f"Failover {world['t']}", "modes": ["keyword"]}, headers=h)
    assert "candidates" in r.json()["variants"][0]["diagnostics"]


async def test_a_source_filter_narrows_within_grants(world):
    p = person(("viewer", "*", "*"))
    async with session_scope() as db:
        r = await service.search(db, p, world["t"], 8, sources=[str(world["a"])])
    assert r["results"] and {x["document_id"] for x in r["results"]} == {str(world["ok"])}


async def test_a_resource_and_scope_match_are_visible_signals(world):
    t = world["t"]
    s = await source(world["prod"], "team-a", applies_to={"resources": ["Deployment"]})
    d = await doc(s, "rollout.md", f"# Rollout {t}\nRoll the deployment {t} back.")
    async with session_scope() as db:
        r = await service.search(db, person(("viewer", "*", "*")), f"rollout deployment {t}", 5, environment=world["prod"])
    hit = next(x for x in r["results"] if x["document_id"] == str(d))
    assert "resource_type" in hit["signals"] and "scope" in hit["signals"]


async def test_only_stale_passages_mean_abstaining(world):
    t = world["t"]
    s = await source(world["prod"], "team-a")
    await doc(s, "dep.md", f"---\nstatus: deprecated\n---\n# Procedure {t}\nThe {t} deprecated procedure steps.")
    async with session_scope() as db:
        r = await service.search(db, person(("viewer", world["prod"], "team-a")), f"deprecated procedure steps {t}", 5, sources=[str(s)])
    assert r["evidence"]["state"] == "weak" and r["abstain"] is True and "stale" in r["evidence"]["reason"]


async def test_conflicts_name_the_other_document(world):
    t = world["t"]
    s = await source(world["prod"], "team-a")
    await doc(s, "one.md", f"# Restart {t}\nRestart {t} with the old script.")
    await doc(s, "two.md", f"# Restart {t}\nRestart {t} with the new pipeline.")
    async with session_scope() as db:
        r = await service.search(db, person(("viewer", world["prod"], "team-a")), f"Restart {t}", 5, sources=[str(s)])
    assert {tuple(x["freshness"]["conflict_with"]) for x in r["results"]} == {("one.md",), ("two.md",)}
    assert r["evidence"]["quality"]["possible_conflicts"] == 2


def test_claim_support_is_literal_and_says_so():
    results = [{"chunk_id": "c", "title": "Redis", "heading": "", "text": "Promote the replica with REPLICAOF NO ONE then repoint the sentinel"}]
    good = service.claim_support("promote the replica then repoint the sentinel", results)
    assert good["state"] == "supported" and good["basis"].startswith("literal")
    assert service.claim_support("ERR_PAY_4071 means promote the replica", results)["state"] == "unsupported"


# ---- feedback -----------------------------------------------------------------------------------------


async def test_feedback_is_review_input_and_tied_to_what_was_readable(client, world):
    h = await login(client, world["email"])
    async with session_scope() as db:
        readable = (await db.execute(select(Chunk.id).where(Chunk.document_id == world["ok"]))).scalars().first()
        unreadable = (await db.execute(select(Chunk.id).where(Chunk.document_id == world["hidden"]))).scalars().first()
    assert (await client.post(f"{API}/feedback", json={"query": "x y", "verdict": "outdated", "chunk_id": str(unreadable)}, headers=h)).status_code == 404
    r = await client.post(f"{API}/feedback", json={"query": "token=abc123 failover", "verdict": "outdated", "chunk_id": str(readable), "note": "old", "mode": "hybrid"}, headers=h)
    assert r.status_code == 201
    async with session_scope() as db:
        row = await db.get(RetrievalFeedback, uuid.UUID(r.json()["id"]))
        assert "abc123" not in row.query and row.state == "open"


async def test_missing_feedback_needs_a_readable_scope(client, world):
    h = await login(client, world["email"])
    assert (await client.post(f"{API}/feedback", json={"query": "nothing here", "verdict": "missing", "environment": "nowhere"}, headers=h)).status_code == 404
    assert (await client.post(f"{API}/feedback", json={"query": "nothing here", "verdict": "missing", "environment": world["prod"]}, headers=h)).status_code == 201
    assert (await client.post(f"{API}/feedback", json={"query": "nothing here", "verdict": "outdated"}, headers=h)).status_code == 422


async def test_a_reviewer_reads_feedback_only_in_scope(client, world):
    h = await login(client, world["email"])
    async with session_scope() as db:
        chunk = (await db.execute(select(Chunk.id).where(Chunk.document_id == world["ok"]))).scalars().first()
    made = (await client.post(f"{API}/feedback", json={"query": "scoped", "verdict": "irrelevant", "chunk_id": str(chunk)}, headers=h)).json()
    inside, outside = f"in-{world['t']}@example.com", f"out-{world['t']}@example.com"
    await make_user(inside, [f"knowledge_reviewer:{world['prod']}:team-a"])
    await make_user(outside, [f"knowledge_reviewer:{world['dev']}:team-b"])
    h2 = await login(client, outside)
    assert made["id"] not in str((await client.get(f"{API}/feedback")).json())
    assert (await client.put(f"{API}/feedback/{made['id']}", json={"state": "reviewed"}, headers=h2)).status_code == 404
    h3 = await login(client, inside)
    assert made["id"] in str((await client.get(f"{API}/feedback")).json())
    assert (await client.put(f"{API}/feedback/{made['id']}", json={"state": "reviewed"}, headers=h3)).status_code == 200
