"""Regression tests for the static review of bf00ffc."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text, update

from moks import detection, jobs, skills
from moks.actions import executor as ex
from moks.api import metrics as m
from moks.api.environments import sync_automation_binding
from moks.connectors import registry
from moks.db import session_scope
from moks.detection import Check, Rule, Signal
from moks.models import ActionPlan, DetectionState, EmbeddingIndex, Environment, Incident, Job, KnowledgeSource
from tests.conftest import login, make_env, make_user
from tests.fakes import OBJECTS, SCENARIOS, FakeKubernetes, FakeWriter, pod, reset_deployments
from tests.test_metrics_workspace import Prom


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "kubernetes", FakeKubernetes)
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "prometheus", Prom)
    registry._cache.clear()
    m._catalog_cache.clear()
    Prom.calls, Prom.fail, Prom.slow = [], set(), set()
    reset_deployments()
    yield
    registry._cache.clear()


def rule(**kw):
    meta = skills.SkillMeta(name="test-skill", description="a rule used in these tests", version="1")
    spec = skills.DetectionSpec(id=f"r-{uuid.uuid4().hex[:6]}", kind="finding", finding="crashloop", sustained_checks=1, recovery_checks=1, cooldown_minutes=0, **kw)
    return Rule(spec, skills.Skill(meta=meta, body="", path="x", source="builtin"))


async def _env():
    name = f"rv-{uuid.uuid4().hex[:6]}"
    env_id = await make_env(name, connectors=[{"type": "kubernetes", "name": "k8s", "config": {}}])
    async with session_scope() as db:
        (await db.get(Environment, env_id)).detection_enabled = True
        await sync_automation_binding(db, name, True)
    return env_id


async def _apply(env_id, r, check):
    async with session_scope() as db:
        return await detection.apply(db, await db.get(Environment, env_id), r, r.spec, check, datetime.now(timezone.utc))


async def _incidents(env_id):
    async with session_scope() as db:
        return (await db.execute(select(Incident).where(Incident.environment_id == env_id).order_by(Incident.created_at))).scalars().all()


def sig(resource="Deployment/api", ns="shop", **kw):
    return Signal(resource, ns, "crashing", evidence=[{"tool": "t"}], **kw)


# ---- 1. Identity -----------------------------------------------------------------------------------

async def test_same_name_in_two_namespaces_is_two_incidents_and_two_states():
    env, r = await _env(), rule()
    await _apply(env, r, Check("firing", [sig(ns="shop"), sig(ns="billing")]))
    rows = await _incidents(env)
    assert sorted(i.namespace for i in rows) == ["billing", "shop"] and len({i.fingerprint for i in rows}) == 2
    async with session_scope() as db:
        assert len((await db.execute(select(DetectionState).where(DetectionState.rule_id == r.id))).scalars().all()) == 2


async def test_kind_is_part_of_identity():
    env, r = await _env(), rule()
    await _apply(env, r, Check("firing", [sig("Deployment/api"), sig("Service/api")]))
    assert len(await _incidents(env)) == 2


async def test_clearing_one_namespace_leaves_the_other_open():
    env, r = await _env(), rule(auto_resolve=True)
    await _apply(env, r, Check("firing", [sig(ns="shop"), sig(ns="billing")]))
    await _apply(env, r, Check("firing", [sig(ns="billing")]))
    assert {i.namespace: i.status for i in await _incidents(env)} == {"shop": "resolved", "billing": "open"}


async def test_a_pod_restart_does_not_open_a_new_incident(monkeypatch):
    env, r = await _env(), rule()
    for pod_name in ("payments-5c4-aaa", "payments-5c4-bbb"):
        monkeypatch.setitem(SCENARIOS, "churn", {"pods": [pod(pod_name, "churn", {"app": "payments"}, waiting="CrashLoopBackOff", last="Error", restarts=7)],
                                                 "services": [], "deployments": [], "logs": "x"})
        async with session_scope() as db:
            e = await db.get(Environment, env)
            check = await detection.evaluate(db, e, r.spec.model_copy(update={"namespaces": ["churn"]}))
            await detection.apply(db, e, r, r.spec, check, datetime.now(timezone.utc))
    rows = await _incidents(env)
    assert len(rows) == 1 and rows[0].resource == "Deployment/payments"


async def test_legacy_incident_is_adopted_and_an_unmatched_one_stays_open():
    env, r = await _env(), rule(auto_resolve=True)
    from moks.models import AUTOMATION_USER_ID

    async with session_scope() as db:
        for resource, fp in (("pod/payments-1", "a"), ("pod/gone", "b")):
            db.add(Incident(title="old", environment_id=env, namespace="shop", source="detection", fingerprint=f"v1-{fp * 40}", rule_id=r.id,
                            resource=resource, created_by=AUTOMATION_USER_ID, status="open"))
    await _apply(env, r, Check("firing", [sig("Deployment/payments", aliases=["pod/payments-1"])]))
    rows = await _incidents(env)
    assert len(rows) == 2  # adopted, not duplicated
    adopted = next(i for i in rows if i.resource == "Deployment/payments")
    assert not adopted.fingerprint.startswith("v1-")
    assert next(i for i in rows if i.resource == "pod/gone").status == "open"


# ---- 2. Coverage ------------------------------------------------------------------------------------

def many_namespaces(monkeypatch, n):
    names = [f"ns-{i:03d}" for i in range(n)]

    async def list_namespaces(self):
        return names
    monkeypatch.setattr(FakeKubernetes, "list_namespaces", list_namespaces)
    for name in names:
        monkeypatch.setitem(SCENARIOS, name, {"pods": [pod("ok-1", name, {"app": "ok"})], "services": [], "deployments": [], "logs": "x"})
    return names


async def _evaluate(env, spec, **kw):
    async with session_scope() as db:
        return await detection.evaluate(db, await db.get(Environment, env), spec, **kw)


async def test_more_than_40_namespaces_are_incomplete_and_rotate_to_cover_all(monkeypatch):
    env, r = await _env(), rule()
    names = many_namespaces(monkeypatch, 45)
    first = await _evaluate(env, r.spec)
    assert not first.complete and len(first.covered) == 40 and first.next_cursor == 40
    second = await _evaluate(env, r.spec, cursor=first.next_cursor)
    assert first.covered | second.covered == set(names)


async def test_namespace_outside_this_slice_is_not_recovered(monkeypatch):
    env, r = await _env(), rule(auto_resolve=True)
    await _apply(env, r, Check("firing", [sig(ns="ns-044")]))
    await _apply(env, r, Check("ok", complete=False, covered={f"ns-{i:03d}" for i in range(40)}))
    assert (await _incidents(env))[0].status == "open"
    await _apply(env, r, Check("ok", complete=False, covered={"ns-044"}))  # the rotation reaches it
    assert (await _incidents(env))[0].status == "resolved"


async def test_more_than_50_firing_resources_clear_nothing(monkeypatch):
    env, r = await _env(), rule(auto_resolve=True)
    await _apply(env, r, Check("firing", [sig("Deployment/victim", ns="zzz")]))
    flood = [sig(f"Deployment/app-{i:03d}", ns="flood") for i in range(60)]

    async def evaluator(spec, ex, cursor=0):
        return Check("firing", list(flood))
    monkeypatch.setattr(detection, "_evaluate_finding", evaluator)
    async with session_scope() as db:
        e = await db.get(Environment, env)
        check = await detection.evaluate(db, e, r.spec)
        assert len(check.signals) == detection.MAX_SIGNALS and not check.complete and check.covered == set()
        await detection.apply(db, e, r, r.spec, check, datetime.now(timezone.utc))
    victim = next(i for i in await _incidents(env) if i.resource == "Deployment/victim")
    assert victim.status == "open"


async def test_a_flood_truncated_by_the_result_size_cap_also_proves_nothing(monkeypatch):
    env, r = await _env(), rule()
    monkeypatch.setitem(SCENARIOS, "flood", {"pods": [pod(f"p{i}-abc-x", "flood", {"app": f"p{i}"}, waiting="CrashLoopBackOff", last="Error", restarts=7) for i in range(60)],
                                             "services": [], "deployments": [], "logs": "x"})
    check = await _evaluate(env, r.spec.model_copy(update={"namespaces": ["flood"]}))
    assert not check.complete and check.covered == set()


async def test_truncated_tool_output_does_not_cover_its_namespace(monkeypatch):
    env, r = await _env(), rule()
    real = FakeKubernetes.list_pods

    async def truncated(self, ns, label_selector=None):
        pods, _ = await real(self, ns, label_selector)
        return pods, True
    monkeypatch.setattr(FakeKubernetes, "list_pods", truncated)
    check = await _evaluate(env, r.spec.model_copy(update={"namespaces": ["shop", "secure"]}))
    assert not check.complete and check.covered == set()


async def test_partial_connector_failure_only_covers_what_answered():
    env, r = await _env(), rule()
    check = await _evaluate(env, r.spec)  # the fake lists kube-system and cannot read it
    assert not check.complete and "kube-system" not in check.covered and "shop" in check.covered


async def test_prometheus_and_loki_limits_are_incomplete():
    class Ex:
        def __init__(self, data, truncated=False):
            self.data, self.truncated = data, truncated

        async def run(self, tool, args):
            from types import SimpleNamespace

            return SimpleNamespace(ok=True, data=self.data, truncated=self.truncated, summary="", collected_at="now")
        ctx = None

    class Scope:
        allows = staticmethod(lambda n: True)

    spec = skills.DetectionSpec(id="p-rule", kind="prometheus", query="up", threshold=5, resource_label=["pod"])
    e = Ex({"series": [{"metric": {"pod": "a"}, "value": [1, 1.0]}]}, truncated=True)
    e.ctx = type("C", (), {"scope": Scope})
    assert not (await detection._evaluate_prometheus(spec, e)).complete
    lines = [{"line": "x", "labels": {"namespace": "shop", "pod": "a"}}] * detection.LOKI_LIMIT
    loki = skills.DetectionSpec(id="l-rule", kind="loki", query="timeout", threshold=1)
    assert not (await detection._evaluate_loki(loki, Ex({"lines": lines}))).complete


# ---- 3 and 5. Sources and connectors -------------------------------------------------------------------

async def _prom_env(*connectors):
    name = f"src-{uuid.uuid4().hex[:6]}"
    await make_env(name, connectors=[{"type": "prometheus", "name": n, "config": {"base_url": url, **cfg}, "credential_ref": cred} for n, url, cfg, cred in connectors])
    return name


NONE = {"type": "none"}


async def _query(client, h, **kw):
    return (await client.post("/moks/api/metrics/query", json={"query": "up", **kw}, headers=h)).json()


async def test_shared_url_with_different_credentials_is_two_sources(client):
    a = await _prom_env(("prom", "http://shared", {}, {"type": "env", "name": "MOKS_CRED_A"}))
    b = await _prom_env(("prom", "http://shared", {}, {"type": "env", "name": "MOKS_CRED_B"}))
    tag = uuid.uuid4().hex[:6]
    await make_user(f"u-{tag}@example.com", [f"investigator:{a}:*", f"investigator:{b}:*"])
    h = await login(client, f"u-{tag}@example.com")
    r = await _query(client, h, environments=f"{a},{b}")
    assert len(r["results"]) == 2


async def test_shared_url_with_different_tenants_is_two_sources(client):
    a = await _prom_env(("prom", "http://shared", {"tenant_id": "a"}, NONE))
    b = await _prom_env(("prom", "http://shared", {"tenant_id": "b"}, NONE))
    tag = uuid.uuid4().hex[:6]
    await make_user(f"u-{tag}@example.com", [f"investigator:{a}:*", f"investigator:{b}:*"])
    h = await login(client, f"u-{tag}@example.com")
    assert len((await _query(client, h, environments=f"{a},{b}"))["results"]) == 2


async def test_shared_prometheus_with_different_namespace_permissions_is_not_reused(client):
    a = await _prom_env(("prom", "http://shared", {}, NONE))
    b = await _prom_env(("prom", "http://shared", {}, NONE))
    tag = uuid.uuid4().hex[:6]
    await make_user(f"u-{tag}@example.com", [f"investigator:{a}:*", f"investigator:{b}:shop"])
    h = await login(client, f"u-{tag}@example.com")
    r = await _query(client, h, environments=f"{a},{b}")
    by_env = {x["source"]["environments"][0]: x for x in r["results"]}
    assert len(r["results"]) == 2 and by_env[a]["ok"] and not by_env[b]["ok"] and by_env[b]["denied"]


async def test_equal_connector_and_access_is_still_queried_once(client):
    a = await _prom_env(("prom", "http://shared", {}, NONE))
    b = await _prom_env(("prom", "http://shared", {}, NONE))
    tag = uuid.uuid4().hex[:6]
    await make_user(f"u-{tag}@example.com", [f"investigator:{a}:*", f"investigator:{b}:*"])
    h = await login(client, f"u-{tag}@example.com")
    r = await _query(client, h, environments=f"{a},{b}")
    assert len(r["results"]) == 1 and len(r["results"][0]["source"]["connector_ids"]) == 2


async def test_every_connector_is_queried_with_provenance_and_can_be_selected(client):
    env = await _prom_env(("app", "http://app", {}, NONE), ("infra", "http://infra", {}, NONE))
    tag = uuid.uuid4().hex[:6]
    await make_user(f"u-{tag}@example.com", [f"investigator:{env}:*"])
    h = await login(client, f"u-{tag}@example.com")
    r = await _query(client, h, environments=env)
    assert sorted(x["source"]["connector"] for x in r["results"]) == ["app", "infra"]
    sources = (await client.get("/moks/api/metrics/sources")).json()["environments"]
    ids = {d["name"]: d["id"] for e in sources for d in e["data_sources"]}
    one = await _query(client, h, environments=env, connectors=[ids["infra"]])
    assert [x["source"]["connector"] for x in one["results"]] == ["infra"]
    assert [c for c in Prom.calls if c[1] == "up" and c[0] == "http://infra"]
    cat = (await client.get("/moks/api/metrics/catalog", params={"environments": env, "connectors": ids["app"]})).json()
    assert cat["total"] == 3


async def test_a_connector_you_cannot_read_looks_like_a_missing_one(client):
    mine = await _prom_env(("prom", "http://mine", {}, NONE))
    other = await _prom_env(("prom", "http://other", {}, NONE))
    tag = uuid.uuid4().hex[:6]
    await make_user(f"a-{tag}@example.com", ["admin"])
    await login(client, f"a-{tag}@example.com")
    sources = (await client.get("/moks/api/metrics/sources")).json()["environments"]
    secret = next(d["id"] for e in sources if e["environment"] == other for d in e["data_sources"])
    await make_user(f"u-{tag}@example.com", [f"investigator:{mine}:*"])
    h = await login(client, f"u-{tag}@example.com")
    for ids in ([secret], [str(uuid.uuid4())]):
        r = await client.post("/moks/api/metrics/query", json={"query": "up", "connectors": ids}, headers=h)
        assert r.status_code == 403 and r.json()["detail"] == "you cannot read one of those data sources"


# ---- 6. Scoped discovery ---------------------------------------------------------------------------------

class ScopedProm(Prom):
    matches: list = []

    async def label_values(self, label, match, start, end, limit=500):
        ScopedProm.matches.append((label, match))
        if label == "namespace":
            return ["shop", "secure"]
        if label == "__name__":
            return {'{namespace="shop"}': ["shop_orders_total"], '{namespace="secure"}': ["vault_secrets_total"]}.get(match, ["shop_orders_total", "vault_secrets_total", "node_load1"])
        return []

    async def metadata(self):
        return {"shop_orders_total": {"type": "counter"}, "vault_secrets_total": {"type": "counter", "help": "secret"}}


@pytest.fixture
def scoped(monkeypatch):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "prometheus", ScopedProm)
    ScopedProm.matches = []


async def test_scoped_user_gets_only_names_from_their_namespaces(client, scoped):
    env = await _prom_env(("prom", "http://p", {}, NONE))
    tag = uuid.uuid4().hex[:6]
    await make_user(f"s-{tag}@example.com", [f"investigator:{env}:shop"])
    await login(client, f"s-{tag}@example.com")
    r = (await client.get("/moks/api/metrics/catalog", params={"environments": env})).json()
    assert [i["name"] for i in r["items"]] == ["shop_orders_total"]
    assert ("__name__", None) not in ScopedProm.matches


async def test_catalog_cache_never_serves_a_wider_users_list(client, scoped):
    env = await _prom_env(("prom", "http://p", {}, NONE))
    tag = uuid.uuid4().hex[:6]
    await make_user(f"w-{tag}@example.com", [f"investigator:{env}:*"])
    await make_user(f"s-{tag}@example.com", [f"investigator:{env}:shop"])
    await login(client, f"w-{tag}@example.com")
    wide = (await client.get("/moks/api/metrics/catalog", params={"environments": env})).json()
    assert len(wide["items"]) == 3
    await login(client, f"s-{tag}@example.com")
    narrow = (await client.get("/moks/api/metrics/catalog", params={"environments": env})).json()
    assert [i["name"] for i in narrow["items"]] == ["shop_orders_total"]
    await login(client, f"w-{tag}@example.com")
    assert len((await client.get("/moks/api/metrics/catalog", params={"environments": env})).json()["items"]) == 3


async def test_metadata_for_everything_is_refused_to_a_scoped_user(client, scoped):
    env = await _prom_env(("prom", "http://p", {}, NONE))
    tag = uuid.uuid4().hex[:6]
    await make_user(f"s-{tag}@example.com", [f"investigator:{env}:shop"])
    await login(client, f"s-{tag}@example.com")
    from fastapi import Request  # noqa: F401
    from moks.rbac import Binding, Principal
    from moks.scope import effective_scope
    from moks.tools.contracts import ToolContext
    from moks.tools.executor import ToolExecutor

    async with session_scope() as db:
        e = (await db.execute(select(Environment).where(Environment.name == env))).scalar_one()
        principal = Principal(str(uuid.uuid4()), "u@x", [Binding("investigator", env, "shop")])
        connectors, _ = await registry.for_environment(db, e.id)
        now = datetime.now(timezone.utc)
        ctx = ToolContext(None, e, effective_scope(principal, e, m.P.EXPLORER_READ), principal, now - timedelta(hours=1), now, connectors, timedelta(hours=6))
        out = await ToolExecutor(None, ctx, read_permissions=(m.P.EXPLORER_READ,)).run("prom_metric_metadata", {})
    assert out.denied


# ---- 4. Background jobs ---------------------------------------------------------------------------------

async def _running(type_, payload, *, attempts=1, expired=True, key=None):
    jid = await jobs.enqueue(type_, payload, dedupe_key=key or f"{type_}:{uuid.uuid4()}")
    async with session_scope() as db:
        await db.execute(update(Job).where(Job.id == jid).values(
            status="running", worker_id="dead-worker", attempts=attempts, started_at=datetime.now(timezone.utc) - timedelta(hours=1),
            lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=-5 if expired else 300)))
    return jid


async def _job(jid):
    async with session_scope() as db:
        return await db.get(Job, jid)


async def test_killed_sync_is_requeued_and_its_dedupe_key_unblocks():
    key = f"knowledge_sync:{uuid.uuid4()}"
    jid = await _running("knowledge_sync", {"source_id": str(uuid.uuid4())}, key=key)
    assert await jobs.enqueue("knowledge_sync", {}, dedupe_key=key) is None  # blocked while "running"
    assert jid in await jobs.reap_abandoned()
    job = await _job(jid)
    assert job.status == "queued" and job.worker_id is None and "stopped responding" in job.error


async def test_periodic_jobs_are_not_retried_after_a_crash_and_the_next_one_can_start():
    key = f"detection_tick:{uuid.uuid4()}"
    jid = await _running("detection_tick", {"environment_id": str(uuid.uuid4())}, key=key)
    await jobs.reap_abandoned()
    assert (await _job(jid)).status == "failed"
    assert await jobs.enqueue("detection_tick", {}, dedupe_key=key) is not None


async def test_retries_stop_at_the_operations_limit_and_the_source_can_sync_again():
    async with session_scope() as db:
        src = KnowledgeSource(name=f"crash-{uuid.uuid4().hex[:6]}", type="github", config={"repository": "a/b"})
        db.add(src)
        await db.flush()
        sid = src.id
    jid = await _running("knowledge_sync", {"source_id": str(sid)}, attempts=jobs.POLICIES["knowledge_sync"].max_attempts)
    await jobs.reap_abandoned()
    assert (await _job(jid)).status == "failed"
    async with session_scope() as db:
        s = await db.get(KnowledgeSource, sid)
        assert s.last_sync_status == "error" and s.next_sync_at is not None


async def test_killed_reindex_releases_its_build_slot():
    async with session_scope() as db:
        await db.execute(text("DELETE FROM embedding_indexes WHERE backend = 'crash-test'"))
        db.add(EmbeddingIndex(backend="crash-test", model_key="x", index_name="i", status="building"))
    await _running("knowledge_reindex", {})
    await jobs.reap_abandoned()
    async with session_scope() as db:
        row = (await db.execute(select(EmbeddingIndex).where(EmbeddingIndex.backend == "crash-test"))).scalar_one()
        assert row.status == "failed" and row.cleanup_pending
        await db.delete(row)


async def test_a_live_lease_is_left_alone():
    jid = await _running("knowledge_sync", {}, expired=False)
    assert jid not in await jobs.reap_abandoned()
    assert (await _job(jid)).status == "running"


async def test_a_job_with_no_lease_from_before_the_upgrade_is_reclaimed():
    jid = await _running("alert_poll", {})
    async with session_scope() as db:
        await db.execute(update(Job).where(Job.id == jid).values(lease_expires_at=None))
    assert jid in await jobs.reap_abandoned()


async def test_claim_sets_a_lease_and_a_restarted_worker_can_claim_the_retry():
    jid = await _running("knowledge_sync", {"source_id": str(uuid.uuid4())})
    await jobs.reap_abandoned()
    async with session_scope() as db:
        await db.execute(update(Job).where(Job.id == jid).values(run_after=datetime.now(timezone.utc)))
    while (claimed := await jobs.claim("new-worker")) != jid:
        assert claimed
    job = await _job(jid)
    assert job.lease_expires_at > datetime.now(timezone.utc) and job.worker_id == "new-worker"
    async with session_scope() as db:
        await db.execute(update(Job).where(Job.id == jid).values(status="failed"))


async def test_a_worker_that_lost_its_lease_stops_and_cannot_overwrite_the_result(monkeypatch):
    monkeypatch.setattr(jobs, "LEASE_SECONDS", 0.03)
    started = asyncio.Event()

    async def slow(db, payload):
        started.set()
        await asyncio.sleep(5)
        return {"late": True}
    monkeypatch.setattr(jobs, "handlers", lambda: {"alert_poll": slow})
    jid = await _running("alert_poll", {}, expired=False)
    run = asyncio.create_task(jobs.run_one(jid))
    await started.wait()
    async with session_scope() as db:  # another worker took over
        await db.execute(update(Job).where(Job.id == jid).values(worker_id="new-worker"))
    await asyncio.wait_for(run, 2)
    job = await _job(jid)
    assert job.result is None and job.status == "running" and job.worker_id == "new-worker"
    async with session_scope() as db:
        await db.execute(update(Job).where(Job.id == jid).values(status="failed"))


async def test_heartbeat_renews_a_long_job(monkeypatch):
    monkeypatch.setattr(jobs, "LEASE_SECONDS", 0.06)

    async def slow(db, payload):
        await asyncio.sleep(0.15)
        return {"ok": True}
    monkeypatch.setattr(jobs, "handlers", lambda: {"alert_poll": slow})
    jid = await _running("alert_poll", {}, expired=False)
    first = (await _job(jid)).heartbeat_at
    run = asyncio.create_task(jobs.run_one(jid))
    await asyncio.sleep(0.1)
    assert (await _job(jid)).heartbeat_at != first
    await run
    assert (await _job(jid)).status == "succeeded"


# ---- Explorer management: delete_pod ----------------------------------------------------------------------

async def _action_env():
    name = f"act-{uuid.uuid4().hex[:6]}"
    env_id = await make_env(name, connectors=[{"type": "kubernetes", "name": "k8s", "config": {"mode": "in_cluster", "executor": {"mode": "in_cluster"}}}])
    tag = uuid.uuid4().hex[:6]
    await make_user(f"op-{tag}@example.com", [f"operator:{name}:shop"])
    return name, f"op-{tag}@example.com", env_id


async def _delete_plan(client, h, env, pod_name="checkout-abc"):
    return await client.post("/moks/api/actions/plans", json={"environment": env, "namespace": "shop", "action": "delete_pod", "target_kind": "Pod",
                                                              "target_name": pod_name, "params": {}, "reason": "pod is wedged"}, headers=h)


async def _run_plan(writer):
    async def factory(env, connector_id=None):
        return writer
    ex.writer_factory = factory
    claim = await ex.claim_plan("t")
    pid = claim.plan_id
    await ex.execute_plan(claim)
    async with session_scope() as db:
        return await db.get(ActionPlan, pid)


async def test_delete_pod_replaces_a_managed_pod(client, monkeypatch):
    monkeypatch.setattr(ex, "POLL", 0.01)
    env, op, _ = await _action_env()
    h = await login(client, op)
    plan = (await _delete_plan(client, h, env)).json()
    assert plan["auto_recover"] is False and "cannot be restored" in plan["rollback"]
    await client.post(f"/moks/api/actions/plans/{plan['id']}/execute", json={"plan_hash": plan["plan_hash"]}, headers=h)
    w = FakeWriter()
    done = await _run_plan(w)
    assert done.status == "succeeded" and ("delete_pod", "shop", "checkout-abc", "uid-pod") in w.calls


async def test_a_pod_nothing_would_replace_cannot_be_deleted(client):
    env, op, _ = await _action_env()
    h = await login(client, op)
    r = await _delete_plan(client, h, env, "bare")
    assert r.status_code == 403 and "no controller" in r.json()["detail"]


async def test_a_recreated_pod_is_not_deleted(client):
    env, op, _ = await _action_env()
    h = await login(client, op)
    plan = (await _delete_plan(client, h, env)).json()
    await client.post(f"/moks/api/actions/plans/{plan['id']}/execute", json={"plan_hash": plan["plan_hash"]}, headers=h)
    OBJECTS[("shop", "Pod", "checkout-abc")]["metadata"]["uid"] = "someone-else"
    w = FakeWriter()
    done = await _run_plan(w)
    assert done.status == "failed" and "different pod" in done.result["error"] and not w.calls
