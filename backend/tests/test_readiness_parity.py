"""Readiness counts what search returns. Each case changes one document and checks that the source's
`eligible` count and the documents an all-reading person actually retrieves move together."""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from moks.db import session_scope
from moks.models import Document, KnowledgeSource
from moks.rag import readiness
from tests.conftest import make_user, login
from tests.test_rag_governance import doc, find, person, source

NOW = datetime.now(timezone.utc)


async def health(sid):
    async with session_scope() as db:
        return await readiness.source_health(db, await db.get(KnowledgeSource, sid))


async def returned(p, word):
    r = await find(p, word, k=20)
    return {x["document_id"] for x in r["results"]}, {x["document_id"]: x["freshness"]["state"] for x in r["results"]}


async def setup(policy="exclude", **src_kw):
    tag = uuid.uuid4().hex[:6]
    env, word = f"par-{tag}", f"ocelot{tag}"
    sid = await source(env, "*", stale_policy=policy, stale_after_days=30, **src_kw)
    ids = {}
    for name in ("current", "unchecked", "review_overdue", "quarantined", "superseded", "deprecated"):
        ids[name] = await doc(sid, f"{name}.md", f"# {word} {name}\n{word} runbook text about {name} {uuid.uuid4().hex}")
    async with session_scope() as db:
        await db.execute(update(Document).where(Document.id == ids["unchecked"]).values(checked_at=NOW - timedelta(days=90)))
        await db.execute(update(Document).where(Document.id == ids["review_overdue"]).values(review_due_at=NOW - timedelta(days=1)))
        await db.execute(update(Document).where(Document.id == ids["quarantined"]).values(lifecycle="quarantined"))
        await db.execute(update(Document).where(Document.id == ids["superseded"]).values(doc_state="superseded", superseded_by="current.md"))
        await db.execute(update(Document).where(Document.id == ids["deprecated"]).values(doc_state="deprecated"))
    return sid, env, word, ids


async def test_readiness_and_search_agree_on_stale_quarantined_superseded_and_review_overdue():
    sid, env, word, ids = await setup("exclude")
    got, states = await returned(person(("viewer", env, "*")), word)
    expected = {str(ids[n]) for n in ("current", "superseded", "deprecated")}
    assert got == expected
    assert states[str(ids["superseded"])] == "superseded" and states[str(ids["deprecated"])] == "deprecated"  # returned, labeled
    r = (await health(sid))["retrieval"]
    assert r["ingested"] == 6 and r["indexed"] == 6
    assert r["eligible"] == len(got) == 3
    assert r["excluded"] == {"review": 1, "freshness": 2, "lifecycle": 0}  # unchecked and review-overdue: the review case readiness used to miss
    assert r["flagged"]["superseded"] == 1 and r["flagged"]["deprecated"] == 1
    assert (await health(sid))["searchable"]["documents"] == 3


async def test_flag_policy_keeps_stale_documents_and_readiness_says_so():
    sid, env, word, ids = await setup("flag")
    got, states = await returned(person(("viewer", env, "*")), word)
    assert str(ids["unchecked"]) in got and states[str(ids["unchecked"])] == "stale" and str(ids["quarantined"]) not in got
    r = (await health(sid))["retrieval"]
    assert r["eligible"] == len(got) == 5 and r["excluded"]["freshness"] == 0 and r["flagged"]["stale"] == 2


async def test_a_source_review_overdue_excludes_everything_current_and_both_sides_agree():
    sid, env, word, ids = await setup("exclude", review_interval_days=7, last_reviewed_at=NOW - timedelta(days=30))
    got, _ = await returned(person(("viewer", env, "*")), word)
    r = (await health(sid))["retrieval"]
    assert got == {str(ids["superseded"]), str(ids["deprecated"])} and r["eligible"] == 2  # retired documents are never stale


async def test_a_quarantined_source_and_an_archived_document_count_as_lifecycle():
    sid, env, word, ids = await setup("exclude")
    async with session_scope() as db:
        await db.execute(update(Document).where(Document.id == ids["current"]).values(lifecycle="archived"))
    got, _ = await returned(person(("viewer", env, "*")), word)
    r = (await health(sid))["retrieval"]
    assert str(ids["current"]) not in got and r["excluded"]["lifecycle"] == 1 and r["eligible"] == len(got)
    async with session_scope() as db:
        await db.execute(update(KnowledgeSource).where(KnowledgeSource.id == sid).values(lifecycle="quarantined"))
    got, _ = await returned(person(("viewer", env, "*")), word)
    r = (await health(sid))["retrieval"]
    assert not got and r["eligible"] == 0


async def test_an_evaluation_realm_source_is_never_eligible_in_the_operational_realm():
    tag = uuid.uuid4().hex[:6]
    env, word = f"par-{tag}", f"lynx{tag}"
    sid = await source(env, "*", realm="evaluation")
    await doc(sid, "e.md", f"# {word}\n{word} evaluation fixture")
    got, _ = await returned(person(("viewer", env, "*")), word)
    r = (await health(sid))["retrieval"]
    assert not got and r["eligible"] == 0 and r["excluded"]["lifecycle"] == 1


async def test_hints_count_freshness_exclusions_only_for_readable_sources():
    sid, env, word, _ = await setup("exclude")
    reader = person(("viewer", env, "team"))
    async with session_scope() as db:
        before = await readiness.hints(db, reader, env, "team")
    assert any("have documents past their freshness or review limit" in h for h in before), before
    assert any("waiting for review" in h for h in before), before
    hidden = await source(env, "secret-ns", stale_policy="exclude", stale_after_days=1)  # a source the reader cannot read
    await doc(hidden, "h.md", f"# {word}\nhidden")
    async with session_scope() as db:
        await db.execute(update(Document).where(Document.source_id == hidden).values(checked_at=NOW - timedelta(days=9), lifecycle="quarantined"))
        after = await readiness.hints(db, reader, env, "team")
    assert after == before  # neither its name nor its counts reach this reader


async def test_the_health_api_returns_the_counts_to_source_administrators_only(client):
    sid, env, word, _ = await setup("exclude")
    tag = uuid.uuid4().hex[:6]
    await make_user(f"rd-{tag}@example.com", [f"viewer:{env}:*"])
    h = await login(client, f"rd-{tag}@example.com")
    assert (await client.get(f"/moks/api/knowledge/sources/{sid}/health", headers=h)).status_code == 403


async def test_unreadable_sources_cannot_crowd_a_readers_sources_out_of_the_hints():
    sid, env, word, _ = await setup("exclude")
    async with session_scope() as db:
        for i in range(210):  # sort before the reader's source and are not readable by them
            db.add(KnowledgeSource(name=f"aaa-{uuid.uuid4().hex[:10]}", type="upload", scope_environment=env, scope_namespace=f"other-{i}"))
        out = await readiness.hints(db, person(("viewer", env, "team")), env, "team")
    assert any("past their freshness or review limit" in h for h in out), out
