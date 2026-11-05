"""Learning records, reviewed labels, leakage-free datasets, and similar-incident discovery.

Real PostgreSQL, real investigation engine against the fake cluster (payments-5c4-xyz crash-loops in shop),
real knowledge index for postmortems. No model.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from moks.db import session_scope
from moks.ml import learning, similar
from moks.models import Environment, Incident, Investigation, LearningRecord, MLCapability, User
from moks.rag import ingest
from moks.rbac import Binding, Principal
from tests.conftest import login, make_env, make_user
from tests.test_investigation_graph import cluster  # noqa: F401
from tests.test_knowledge_retrieval import ask, environment, no_model

NOW = datetime.now(timezone.utc)


def person(*bindings):
    return Principal(str(uuid.uuid4()), "p@x", [Binding(r, e, n) for r, e, n in bindings])


async def approved_incident(env: str, namespace: str, *, title: str, cause: str, resolution: str, features: dict, verified=True, days_ago=10, service=None) -> uuid.UUID:
    """An incident whose postmortem was approved: the postmortem document plus its learning record."""
    async with session_scope() as db:
        e = (await db.execute(select(Environment).where(Environment.name == env))).scalar_one()
        owner = (await db.execute(select(User).limit(1))).scalar_one()
        inc = Incident(title=title, environment_id=e.id, namespace=namespace, status="resolved", created_by=owner.id, resource=f"deployment/{service}" if service else None,
                       first_observed_at=NOW - timedelta(days=days_ago), resolved_at=NOW - timedelta(days=days_ago - 1), postmortem_status="approved",
                       postmortem={"root_cause": cause, "resolution": resolution, "root_cause_verified": verified})
        db.add(inc)
        await db.flush()
        await ingest.add_postmortem(db, inc, env, f"# Postmortem: {title}\n\n{cause}\n\nResolution: {resolution}\n", "https://example.test/moks")
        rec = await learning.capture_incident(db, inc, env, owner.id)
        rec.features = {**rec.features, **features}
        return inc.id


async def set_mode(name, mode):
    async with session_scope() as db:
        await db.execute(update(MLCapability).where(MLCapability.name == name).values(mode=mode))


# ---- learning records ---------------------------------------------------------------------------------


async def test_a_finished_investigation_writes_a_record_that_does_not_claim_resolution(client, cluster):  # noqa: F811
    await no_model()
    env, email = await environment()
    inv = await ask(client, env, email)
    async with session_scope() as db:
        rec = (await db.execute(select(LearningRecord).where(LearningRecord.investigation_id == inv.id))).scalar_one()
    assert rec.kind == "investigation" and rec.realm == "operational" and rec.label_state == "unreviewed" and rec.label == {}
    assert rec.outcome["investigation_status"] == "completed" and rec.outcome["resolution"] == "unknown"
    assert "crashloop" in rec.features["symptoms"] and "payments" in rec.features["services"]
    assert set(rec.retrieval) >= {"retrieved", "eligible", "supplied", "cited"}
    assert rec.versions["feature_schema"] == learning.FEATURE_SCHEMA and rec.expires_at is not None
    assert all(r["note"].startswith("steps ran") for r in rec.skills["executed"])  # an executed skill is not a success label
    assert "kubectl" not in json.dumps(rec.features)  # identifiers and kinds only, never evidence text


async def test_reruns_keep_a_reviewers_label_and_review_needs_a_cause(client, cluster):  # noqa: F811
    await no_model()
    env, email = await environment()
    inv = await ask(client, env, email)
    async with session_scope() as db:
        rec = (await db.execute(select(LearningRecord).where(LearningRecord.investigation_id == inv.id))).scalar_one()
        reviewer = (await db.execute(select(User).where(User.email == email))).scalar_one()
        with pytest.raises(ValueError, match="confirmed cause"):
            await learning.review(db, rec, reviewer.id, approve=True)
        await learning.review(db, rec, reviewer.id, approve=True, confirmed_cause="PAYMENTS_DB_URL missing; password=hunter2", resolution="restored the ConfigMap", verification="verified")
        row = await db.get(Investigation, inv.id)
        await learning.capture(db, row, env, inv.report, knowledge=inv.report["knowledge"], eligible=[], versions={})
        await db.refresh(rec)
    assert rec.label_state == "approved" and "hunter2" not in rec.label["confirmed_cause"]  # redacted
    assert rec.outcome["resolution"] == "confirmed"


async def test_evaluation_runs_are_kept_in_their_own_realm(client, cluster):  # noqa: F811
    await no_model()
    env, email = await environment()
    inv = await ask(client, env, email)
    async with session_scope() as db:
        row = await db.get(Investigation, inv.id)
        row.trigger = "eval"
        rec = await learning.capture(db, row, env, inv.report, knowledge={}, eligible=[], versions={})
    assert rec.realm == "evaluation"


async def test_judgments_are_one_per_person_and_target_and_reopen_when_changed():
    await make_user(f"j-{uuid.uuid4().hex[:6]}@example.com", ["viewer"])
    async with session_scope() as db:
        uid = (await db.execute(select(User).limit(1))).scalar_one().id
        a = await learning.judge(db, uid, target_kind="similar_incident", target_id="inc-1", verdict="similar", context_id="inv-1")
        a.state = "reviewed"
        b = await learning.judge(db, uid, target_kind="similar_incident", target_id="inc-1", verdict="not_similar", context_id="inv-1")
        assert a.id == b.id and b.verdict == "not_similar" and b.state == "open"
        with pytest.raises(ValueError):
            await learning.judge(db, uid, target_kind="similar_incident", target_id="inc-1", verdict="relevant")


# ---- datasets --------------------------------------------------------------------------------------------


def rec(i, incident=None, feats=None, day=0, label=None, state="approved"):
    r = LearningRecord(id=uuid.UUID(int=i), kind="investigation", environment="e", namespace="n", incident_id=incident, features=feats or {"symptoms": [f"s{i}"]},
                       label=label or {"confirmed_cause": f"c{i}"}, label_state=state, event_from=NOW + timedelta(days=day), created_at=NOW + timedelta(days=day), retrieval={})
    r.content_hash = learning._content_hash(r.features, r.label)
    return r


def test_a_split_keeps_each_incident_on_one_side_and_drops_duplicates():
    inc = uuid.uuid4()
    rows = [rec(1, inc), rec(2, inc), rec(3, inc)] + [rec(10 + i) for i in range(12)] + [rec(99, feats={"symptoms": ["s10"]}, label={"confirmed_cause": "c10"})]
    sides, info = learning.split(rows, eval_fraction=0.3, seed=3)
    assert len({sides[uuid.UUID(int=i)] for i in (1, 2, 3)}) == 1
    assert info["duplicates_dropped"] == 1 and uuid.UUID(int=99) not in sides
    by = {r.id: r for r in rows}
    assert learning.check_leakage([(s, learning.group_key(by[i]), by[i].content_hash) for i, s in sides.items()]) == []


def test_a_chronological_split_puts_the_latest_groups_in_eval():
    rows = [rec(i, day=i) for i in range(10)]
    sides, _ = learning.split(rows, method="chronological", eval_fraction=0.2)
    assert [i for i in range(10) if sides[uuid.UUID(int=i)] == "eval"] == [8, 9]


def test_leakage_is_detected():
    assert learning.check_leakage([("train", "incident:a", "h1"), ("eval", "incident:a", "h2")]) == ["group incident:a is in both train and eval"]
    assert learning.check_leakage([("train", "g1", "h"), ("eval", "g2", "h")]) == ["content h is in both train and eval"]


async def test_datasets_use_approved_records_only_and_export_redacted_features():
    env = f"ds-{uuid.uuid4().hex[:6]}"
    await make_env(env)
    await make_user(f"{env}@example.com", ["admin"])
    async with session_scope() as db:
        for i in range(6):
            db.add(LearningRecord(schema_version=1, kind="investigation", environment=env, namespace="shop", features={"symptoms": [f"k{i}"], "signature": ["token=abcd1234secret"]},
                                  label={"confirmed_cause": f"cause {i} api_key=sk-live-123456789"} if i < 4 else {}, label_state="approved" if i < 4 else "unreviewed",
                                  fingerprint=uuid.uuid4().hex, content_hash=uuid.uuid4().hex, event_from=NOW + timedelta(hours=i)))
        db.add(LearningRecord(schema_version=1, kind="investigation", environment=env, namespace="shop", realm="evaluation", features={}, label={"confirmed_cause": "x"}, label_state="approved",
                              fingerprint=uuid.uuid4().hex, content_hash=uuid.uuid4().hex))
    async with session_scope() as db:
        ds = await learning.build_dataset(db, name=f"skills-{env}", purpose="skill_ranking", created_by=None, environment=env)
        rows = await learning.export_rows(db, ds)
    assert ds.counts["records"] == 4 and ds.version == 1 and ds.counts["train"] + ds.counts["eval"] == 4
    body = json.dumps(rows)
    assert "sk-live-123456789" not in body and "unreviewed" not in body and all(r["record"]["label"] for r in rows)
    async with session_scope() as db:
        with pytest.raises(ValueError, match="no approved records"):
            await learning.build_dataset(db, name="none", purpose="incident_similarity", created_by=None, environment=env)


async def test_retention_purges_expired_records_but_never_approved_incidents():
    env = f"rt-{uuid.uuid4().hex[:6]}"
    async with session_scope() as db:
        old = LearningRecord(schema_version=1, kind="investigation", environment=env, features={}, fingerprint=uuid.uuid4().hex, content_hash="x", expires_at=NOW - timedelta(days=1))
        keep = LearningRecord(schema_version=1, kind="incident", environment=env, features={}, fingerprint=uuid.uuid4().hex, content_hash="y", label_state="approved", expires_at=None)
        db.add_all([old, keep])
    async with session_scope() as db:
        assert (await learning.purge(db))["records"] >= 1
        left = (await db.execute(select(LearningRecord).where(LearningRecord.environment == env))).scalars().all()
    assert [r.kind for r in left] == ["incident"]


# ---- similar incidents ----------------------------------------------------------------------------------


CRASH = {"symptoms": ["crashloop", "crash_log_reason"], "services": ["payments"], "resource_kinds": ["pod"], "signature": ["payments_db_url", "missing", "exit"]}


async def test_similar_incidents_rank_the_true_match_above_a_hard_negative_and_explain_both():
    env = f"si-{uuid.uuid4().hex[:6]}"
    await make_env(env)
    await make_user(f"{env}@example.com", ["admin"])
    true = await approved_incident(env, "shop", title="payments crash loop: missing DB URL", cause="PAYMENTS_DB_URL was removed from the ConfigMap",
                                   resolution="restored payments-config", features=CRASH, service="payments")
    # Same symptom, different cause, different service and signature: the hard negative
    neg = await approved_incident(env, "shop", title="billing crash loop: ledger lock", cause="ledger lock held after failover", resolution="ran billing-ctl unlock",
                                  features={"symptoms": ["crashloop"], "services": ["billing"], "resource_kinds": ["pod"], "signature": ["ledger", "lock", "held"]}, service="billing")
    async with session_scope() as db:
        r = await similar.find(db, person(("viewer", env, "*")), CRASH, environment=env, namespace="shop")
    ids = [x["incident_id"] for x in r["results"]]
    assert ids[0] == str(true) and (str(neg) not in ids or ids.index(str(neg)) > 0)
    top = r["results"][0]
    assert "crashloop" in top["matching"]["symptoms"] and top["matching"]["services"] == ["payments"]
    assert top["resolution"]["cause_verified"] and "restored" in top["resolution"]["resolution"] and top["age_days"] >= 9
    assert top["citation"]["url"].endswith(f"/incidents/{true}") and r["note"] == similar.NOTE
    if str(neg) in ids:
        n = r["results"][ids.index(str(neg))]
        assert n["differences"]["services_then"] == ["billing"]


async def test_similar_incidents_never_return_what_the_person_cannot_read():
    env = f"sx-{uuid.uuid4().hex[:6]}"
    await make_env(env)
    await make_user(f"{env}@example.com", ["admin"])
    await approved_incident(env, "secret-ns", title="secret crash", cause="x", resolution="y", features=CRASH, service="payments")
    async with session_scope() as db:
        mine = await similar.find(db, person(("viewer", env, "shop")), CRASH, environment=env, namespace=None)
        other_env = await similar.find(db, person(("viewer", "elsewhere", "*")), CRASH, environment="elsewhere", namespace=None)
        wider = await similar.find(db, person(("viewer", env, "*")), CRASH, environment=env, namespace="shop")  # outside the request's namespace
    assert mine["results"] == [] and mine["considered"] == 0 and other_env["results"] == [] and wider["results"] == []


async def test_structure_still_works_when_text_search_fails_or_has_no_embeddings(monkeypatch):
    env = f"sf-{uuid.uuid4().hex[:6]}"
    await make_env(env)
    await make_user(f"{env}@example.com", ["admin"])
    inc = await approved_incident(env, "shop", title="t", cause="c", resolution="r", features=CRASH, service="payments")
    from moks.rag import service

    async def broken(*a, **k):
        raise RuntimeError("index down")
    monkeypatch.setattr(service, "search", broken)
    async with session_scope() as db:
        r = await similar.find(db, person(("viewer", env, "*")), CRASH, environment=env, namespace="shop")
    assert r["results"][0]["incident_id"] == str(inc) and r["text"]["used"] is False and "structural similarity only" in r["text"]["reason"]

    async def keyword_only(*a, **k):
        return {"results": [], "mode": "keyword", "fallback_reason": "embedding provider unavailable"}
    monkeypatch.setattr(service, "search", keyword_only)
    async with session_scope() as db:
        r = await similar.find(db, person(("viewer", env, "*")), CRASH, environment=env, namespace="shop")
    assert r["text"]["note"] == "embeddings unavailable: text matching is keyword-only" and r["results"]


async def test_the_kill_switch_turns_similar_incidents_off():
    env = f"sk-{uuid.uuid4().hex[:6]}"
    await set_mode("similar_incidents", "disabled")
    try:
        async with session_scope() as db:
            r = await similar.find(db, person(("viewer", env, "*")), CRASH, environment=env, namespace="shop")
    finally:
        await set_mode("similar_incidents", "enabled")
    assert r["status"] == "disabled" and r["results"] == []


async def test_an_investigation_lists_similar_incidents_as_historical_leads(client, cluster):  # noqa: F811
    await no_model()
    env, email = await environment()
    past = await approved_incident(env, "shop", title="payments crash loop", cause="missing PAYMENTS_DB_URL", resolution="restored the ConfigMap",
                                   features={"symptoms": ["crashloop"], "services": ["payments"], "resource_kinds": ["pod"], "signature": []}, service="payments")
    inv = await ask(client, env, email)
    s = inv.report["similar_incidents"]
    assert s["status"] == "ok" and s["results"][0]["incident_id"] == str(past) and "does not show the current cause" in s["note"]
    async with session_scope() as db:
        from moks.models import Evidence

        ev = (await db.execute(select(Evidence).where(Evidence.investigation_id == inv.id, Evidence.tool == "similar_incidents"))).scalars().all()
    assert len(ev) == 1 and ev[0].kind == "documentation"  # bounded to one call, and never an observation
    assert not any(ev[0].ref in f["evidence"] for f in inv.report["observed_facts"])


async def test_a_disabled_capability_makes_no_call_and_says_so(client, cluster):  # noqa: F811
    await no_model()
    env, email = await environment()
    await set_mode("similar_incidents", "disabled")
    try:
        inv = await ask(client, env, email)
    finally:
        await set_mode("similar_incidents", "enabled")
    assert inv.report["similar_incidents"]["status"] == "disabled"
    h = await login(client, email)
    r = await client.get(f"/moks/api/investigations/{inv.id}", headers=h)
    assert r.status_code == 200


# ---- evaluation -----------------------------------------------------------------------------------------------


async def test_the_similar_incident_evaluation_runs_in_its_own_realm_and_cleans_up():
    from moks.evals.runner import load
    from moks.ml import evaluation

    out = await evaluation.run_similar(load("similar_incidents"), uuid.uuid4())
    by = {r["case"]: r for r in out["results"]}
    assert by["same-dburl-after-deploy"]["ranked"][0] == "pay-dburl"
    assert by["probe-other-namespace"]["ranked"] == ["web-probe"]  # other namespaces' incidents are outside the request's scope
    assert all(g["evidence"] == "synthetic fixture" for g in out["gates"]) and out["metrics"]["cases"] == 5
    assert out["metrics"]["hard_negative_cases"] == 3
    async with session_scope() as db:
        left = (await db.execute(select(LearningRecord).where(LearningRecord.realm == "evaluation", LearningRecord.environment.like("eval-similar-%")))).scalars().all()
    assert left == []


async def test_reviewed_judgments_become_ranking_cases(client, cluster):  # noqa: F811
    from moks.ml import evaluation

    await no_model()
    env, email = await environment()
    good = await approved_incident(env, "shop", title="t", cause="c", resolution="r", features={"symptoms": ["crashloop"], "services": ["payments"], "resource_kinds": ["pod"], "signature": []}, service="payments")
    inv = await ask(client, env, email)
    async with session_scope() as db:
        uid = (await db.execute(select(User).where(User.email == email))).scalar_one().id
        j = await learning.judge(db, uid, target_kind="similar_incident", target_id=str(good), verdict="similar", context_id=str(inv.id), environment=env, namespace="shop")
        j.state = "reviewed"
    out = await evaluation.reviewed_similar(k=3)
    assert out["cases"] >= 1 and out["source"] == "reviewed operator judgments"
