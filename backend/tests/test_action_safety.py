"""Patch policy, write-target binding, execution-time re-checks, conflict-aware recovery, crash
reconciliation and honest validation."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from moks.actions import executor as ex
from moks.actions.catalog import check_patch, merge_diff, paths_patch
from moks.connectors import registry
from moks.connectors.base import ConnectorError
from moks.db import session_scope
from moks.models import ActionPlan, Approval, Connector, Environment, RoleBinding, User
from tests.conftest import login, make_env, make_user
from tests.fakes import DEPLOYMENTS, OBJECTS, FakeKubernetes, FakeWriter, reset_deployments


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "kubernetes", FakeKubernetes)
    registry._cache.clear()
    reset_deployments()
    monkeypatch.setattr(ex, "POLL", 0.01)
    monkeypatch.setattr(ex, "VALIDATION_TIMEOUT", 1)
    monkeypatch.setattr(ex, "OBJECT_SETTLE_SECONDS", 0.2)
    yield
    registry._cache.clear()


def env_policy(max_replicas=5, zero=False):
    return Environment(name="e", max_replicas=max_replicas, allow_scale_to_zero=zero)


async def _setup(prod=False, config=None):
    name = f"saf-{uuid.uuid4().hex[:6]}"
    cfg = config if config is not None else {"mode": "in_cluster", "executor": {"mode": "in_cluster"}}
    env_id = await make_env(name, connectors=[{"type": "kubernetes", "name": "k8s", "config": cfg}])
    async with session_scope() as db:
        e = await db.get(Environment, env_id)
        e.is_production, e.max_replicas = prod, 5
    tag = uuid.uuid4().hex[:6]
    op, ap = f"sop-{tag}@example.com", f"sap-{tag}@example.com"
    await make_user(op, [f"operator:{name}:shop"])
    await make_user(ap, [f"approver:{name}:shop"])
    return name, op, ap, env_id


async def _plan(client, h, env, action="scale_deployment", params=None, kind="Deployment", **kw):
    body = {"environment": env, "namespace": "shop", "action": action, "target_kind": kind, "target_name": "checkout",
            "params": {"replicas": 4} if params is None else params, "reason": "reviewed change", **kw}
    return await client.post("/moks/api/actions/plans", json=body, headers=h)


async def _approve_and_queue(client, env_tuple, plan):
    _, op, ap, _ = env_tuple
    if plan["requires_approval"]:
        h = await login(client, ap)
        r = await client.post(f"/moks/api/actions/plans/{plan['id']}/approve", json={"plan_hash": plan["plan_hash"]}, headers=h)
        assert r.status_code == 200, r.text
    h = await login(client, op)
    r = await client.post(f"/moks/api/actions/plans/{plan['id']}/execute", json={"plan_hash": plan["plan_hash"]}, headers=h)
    assert r.status_code == 200, r.text


async def _run(writer):
    async def factory(env, connector_id=None):
        return writer

    ex.writer_factory = factory
    claim = await ex.claim_plan("test-exec")
    assert claim
    pid = claim.plan_id
    await ex.execute_plan(claim)
    return await _get(pid)


async def _get(pid):
    async with session_scope() as db:
        return await db.get(ActionPlan, pid)


# ---- 1. Patch policy ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind,patch", [
    ("Deployment", {"spec": {"replicas": 500}}),                                     # bypasses max_replicas
    ("Deployment", {"spec": {"replicas": 0}}),                                       # bypasses allow_scale_to_zero
    ("Deployment", {"spec": {"template": None}}),                                    # null parent
    ("Deployment", {"spec": None}),
    ("Deployment", {"spec": {"template": "x"}}),                                     # scalar parent
    ("Deployment", {"spec": {"template": {"spec": {"containers": [{"name": "a"}]}}}}),  # array replace drops securityContext
    ("Deployment", {"spec": {"template": {"spec": {"tolerations": [{"operator": "Exists"}]}}}}),
    ("Deployment", {"spec": {"template": {"spec": {"dnsPolicy": "None"}}}}),
    ("Deployment", {"spec": {"minReadySeconds": True}}),                             # bool is not an int
    ("Deployment", {"spec": {"minReadySeconds": "5"}}),
    ("Deployment", {"metadata": {"labels": {"a": "b"}}}),
    ("Service", {"spec": {"type": "NodePort"}}),
    ("Service", {"spec": {"ports": [{"port": 1}]}}),
    ("Service", {"spec": {"selector": {}}}),
    ("Service", {"spec": {"selector": {"app": None}}}),
    ("Service", {"spec": {"selector": {"bad key!": "x"}}}),
    ("HorizontalPodAutoscaler", {"spec": {"metrics": []}}),
    ("HorizontalPodAutoscaler", {"spec": {"scaleTargetRef": {"name": "other"}}}),
    ("ConfigMap", {"data": {"a": "b"}}),
])
def test_patch_outside_the_schema_is_refused(kind, patch):
    with pytest.raises(ConnectorError):
        check_patch(kind, patch, env_policy())


def test_hpa_patch_is_held_to_environment_policy_on_the_effective_object():
    current = {"spec": {"minReplicas": 2, "maxReplicas": 4}}
    for patch in ({"spec": {"maxReplicas": 50}}, {"spec": {"minReplicas": 0}}, {"spec": {"minReplicas": 9}}):
        with pytest.raises(ConnectorError):
            check_patch("HorizontalPodAutoscaler", patch, env_policy(), current)
    with pytest.raises(ConnectorError, match="above maxReplicas"):
        check_patch("HorizontalPodAutoscaler", {"spec": {"minReplicas": 5}}, env_policy(), current)  # 5 > the current max of 4
    check_patch("HorizontalPodAutoscaler", {"spec": {"maxReplicas": 5}}, env_policy(), current)


def test_supported_patches_pass():
    check_patch("Service", {"spec": {"selector": {"app": "checkout"}}}, env_policy(), {"spec": {"selector": {"app": "x"}}})
    check_patch("Deployment", {"spec": {"strategy": {"rollingUpdate": {"maxSurge": "25%"}}}}, env_policy())


def test_recovery_patch_removes_added_keys_and_leaves_other_fields_alone():
    assert merge_diff({"a": 1, "b": 2}, {"a": 1}) == {"b": None}
    current = {"spec": {"replicas": 4, "paused": True}}
    assert paths_patch([["spec", "replicas"]], current, {"spec": {"replicas": 2}}) == {"spec": {"replicas": 2}}


async def test_generic_patch_cannot_scale_past_policy_through_the_api(client):
    t = await _setup()
    h = await login(client, t[1])
    r = await _plan(client, h, t[0], "apply_config_patch", {"patch": {"spec": {"replicas": 500}}})
    assert r.status_code == 422 and "spec.replicas" in r.json()["detail"]
    r = await _plan(client, h, t[0], "apply_config_patch", {"patch": {"spec": {"maxReplicas": 500}}}, kind="HorizontalPodAutoscaler")
    assert r.status_code == 403


# ---- 2. Write target ---------------------------------------------------------------------------

async def test_no_write_target_means_no_plan(client):
    t = await _setup(config={"mode": "eks", "cluster_name": "prod", "region": "us-east-1"})
    h = await login(client, t[1])
    r = await _plan(client, h, t[0])
    assert r.status_code == 409 and "no write target" in r.json()["detail"]


async def test_default_writer_never_falls_back_to_the_hosting_cluster(client):
    t = await _setup(config={"mode": "eks", "cluster_name": "prod"})
    async with session_scope() as db:
        env = await db.get(Environment, t[3])
    with pytest.raises(ConnectorError, match="no write target"):
        await ex._default_writer(env)


async def test_write_to_a_different_cluster_is_refused_before_mutation(client):
    t = await _setup()
    h = await login(client, t[1])
    plan = (await _plan(client, h, t[0])).json()
    await _approve_and_queue(client, t, plan)
    w = FakeWriter("ok", cluster="cluster-b")
    done = await _run(w)
    assert done.status == "failed" and "not the cluster" in done.result["error"] and not w.calls
    assert done.mutation_state == "none"


async def test_changed_write_target_is_refused(client):
    t = await _setup()
    h = await login(client, t[1])
    plan = (await _plan(client, h, t[0])).json()
    await _approve_and_queue(client, t, plan)
    async with session_scope() as db:
        row = (await db.execute(select(Connector).where(Connector.environment_id == t[3]))).scalar_one()
        row.config = {**row.config, "executor": {"mode": "kubeconfig"}}
    w = FakeWriter()
    done = await _run(w)
    assert done.status == "failed" and "write target changed" in done.result["error"] and not w.calls


async def test_recreated_object_is_not_touched(client):
    t = await _setup()
    h = await login(client, t[1])
    plan = (await _plan(client, h, t[0])).json()
    await _approve_and_queue(client, t, plan)
    DEPLOYMENTS[("shop", "checkout")]["uid"] = "someone-recreated-it"
    w = FakeWriter()
    done = await _run(w)
    assert done.status == "failed" and "different object" in done.result["error"] and not w.calls


# ---- 3. Execution-time policy ------------------------------------------------------------------

async def test_auto_recover_is_bound_by_the_hash(client):
    t = await _setup()
    h = await login(client, t[1])
    plan = (await _plan(client, h, t[0], auto_recover=True)).json()
    await _approve_and_queue(client, t, plan)
    async with session_scope() as db:
        await db.execute(update(ActionPlan).where(ActionPlan.id == uuid.UUID(plan["id"])).values(auto_recover=False))
    done = await _run(FakeWriter())
    assert done.status == "failed" and "hash mismatch" in done.result["error"]


@pytest.mark.parametrize("change,message", [("requester", "requester may no longer"), ("disabled", "requester may no longer"),
                                            ("limit", "current policy"), ("production", "now needs an approval")])
async def test_changes_after_review_stop_execution(client, change, message):
    t = await _setup()
    h = await login(client, t[1])
    plan = (await _plan(client, h, t[0])).json()
    await _approve_and_queue(client, t, plan)
    async with session_scope() as db:
        env = await db.get(Environment, t[3])
        user = (await db.execute(select(User).where(User.email == t[1]))).scalar_one()
        if change == "requester":  # keeps action.execute through a different user being the executor
            other = f"exec-{uuid.uuid4().hex[:6]}@example.com"
        if change == "disabled":
            user.is_active = False
        if change == "limit":
            env.max_replicas = 3
        if change == "production":
            env.is_production = True
    if change == "requester":
        await make_user(other, [f"operator:{t[0]}:shop"])
        async with session_scope() as db:
            exec_user = (await db.execute(select(User).where(User.email == other))).scalar_one()
            await db.execute(update(ActionPlan).where(ActionPlan.id == uuid.UUID(plan["id"])).values(executed_by=exec_user.id))
            for b in (await db.execute(select(RoleBinding).where(RoleBinding.user_id == user.id))).scalars():
                await db.delete(b)
    w = FakeWriter()
    done = await _run(w)
    assert done.status == "failed" and message in done.result["error"] and not w.calls


async def test_a_plan_from_the_previous_hash_version_is_refused(client):
    t = await _setup()
    h = await login(client, t[1])
    plan = (await _plan(client, h, t[0])).json()
    await _approve_and_queue(client, t, plan)
    async with session_scope() as db:
        await db.execute(update(ActionPlan).where(ActionPlan.id == uuid.UUID(plan["id"])).values(hash_version=1))
    done = await _run(FakeWriter())
    assert done.status == "failed" and "older version" in done.result["error"]


# ---- 4. Recovery -------------------------------------------------------------------------------

async def test_recovery_stops_when_someone_changed_the_same_field(client):
    t = await _setup()
    h = await login(client, t[1])
    plan = (await _plan(client, h, t[0], params={"replicas": 4})).json()
    await _approve_and_queue(client, t, plan)
    w = FakeWriter("stuck")

    def human():
        DEPLOYMENTS[("shop", "checkout")]["replicas"] = 5  # after MOKS's read-back, before recovery
    w.real_get = w.get_object

    async def get_object(ns, kind, name):
        o = await w.real_get(ns, kind, name)
        if any(c[0] == "scale" for c in w.calls) and w.after_mutation is None and getattr(w, "seen", False):
            human()
            o = await w.real_get(ns, kind, name)
        w.seen = any(c[0] == "scale" for c in w.calls)
        return o
    w.get_object = get_object
    done = await _run(w)
    assert done.status == "reconciliation_required" and done.result["recovery"]["changed"] == ["spec.replicas"]
    assert DEPLOYMENTS[("shop", "checkout")]["replicas"] == 5  # the human's value stands


async def test_recovery_touches_only_what_moks_changed(client):
    t = await _setup()
    h = await login(client, t[1])
    plan = (await _plan(client, h, t[0], params={"replicas": 4})).json()
    await _approve_and_queue(client, t, plan)
    w = FakeWriter("stuck")
    w.after_mutation = lambda: DEPLOYMENTS[("shop", "checkout")].update(
        template={**DEPLOYMENTS[("shop", "checkout")]["template"], "metadata": {"labels": {"app": "checkout"}, "annotations": {"human": "edit"}}})
    done = await _run(w)
    assert done.status == "recovered", done.result
    d = DEPLOYMENTS[("shop", "checkout")]
    assert d["replicas"] == 2 and d["template"]["metadata"]["annotations"] == {"human": "edit"}


async def test_recovery_that_does_not_validate_is_not_reported_as_recovered(client):
    t = await _setup()
    h = await login(client, t[1])
    plan = (await _plan(client, h, t[0], "apply_config_patch", {"patch": {"spec": {"selector": {"app": "wrong"}}}}, kind="Service")).json()
    await _approve_and_queue(client, t, plan)
    w = FakeWriter("no_endpoints")
    w._failing = lambda: True  # endpoints never come back, even after the inverse patch
    done = await _run(w)
    assert done.status == "failed" and done.result["recovery"]["validation"]["ok"] is False


async def test_service_recovery_is_validated_and_restores_the_selector(client):
    t = await _setup()
    h = await login(client, t[1])
    plan = (await _plan(client, h, t[0], "apply_config_patch", {"patch": {"spec": {"selector": {"app": "wrong"}}}}, kind="Service")).json()
    await _approve_and_queue(client, t, plan)
    done = await _run(FakeWriter("no_endpoints"))
    assert done.status == "recovered", done.result
    assert done.result["recovery"]["validation"]["endpoints"]["ready"] == 2
    assert OBJECTS[("shop", "Service", "checkout")]["spec"]["selector"] == {"app": "checkout-v2"}


# ---- 5. Crash recovery -------------------------------------------------------------------------

async def _claimed(client, t):
    h = await login(client, t[1])
    plan = (await _plan(client, h, t[0])).json()
    await _approve_and_queue(client, t, plan)
    claim = await ex.claim_plan("dead-executor")
    assert claim
    return claim.plan_id


async def _expire(pid, state):
    async with session_scope() as db:
        await db.execute(update(ActionPlan).where(ActionPlan.id == pid).values(
            lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1), mutation_state=state,
            checkpoint={"uid": "uid-dep", "before": {"spec.replicas": 2}} if state != "none" else None))


async def test_crash_before_any_mutation_fails_cleanly(client):
    t = await _setup()
    pid = await _claimed(client, t)
    await _expire(pid, "none")
    assert await ex.reconcile_stale() == []
    plan = await _get(pid)
    assert plan.status == "failed" and "no change was attempted" in plan.result["error"]


@pytest.mark.parametrize("state", ["intent", "applied", "unknown"])
async def test_crash_with_uncertain_outcome_is_never_retried(client, state):
    t = await _setup()
    pid = await _claimed(client, t)
    await _expire(pid, state)
    w = FakeWriter()

    async def factory(env, connector_id=None):
        return w
    ex.writer_factory = factory
    assert await ex.reconcile_stale() == [pid]
    plan = await _get(pid)
    assert plan.status == "reconciliation_required" and not w.calls
    assert plan.result["observed"]["unchanged_from_before"] == {"spec.replicas": True}
    assert await ex.claim_plan("next") is None  # nothing picks it up again


async def test_live_lease_is_left_alone(client):
    t = await _setup()
    pid = await _claimed(client, t)
    assert await ex.reconcile_stale() == []
    assert (await _get(pid)).status == "executing"


async def test_lost_connection_during_mutation_needs_reconciliation(client):
    t = await _setup()
    h = await login(client, t[1])
    plan = (await _plan(client, h, t[0])).json()
    await _approve_and_queue(client, t, plan)
    w = FakeWriter()
    w.mutate_error = ConnectorError("timeout", "no answer")
    done = await _run(w)
    assert done.status == "reconciliation_required" and done.mutation_state == "intent"


async def test_refused_mutation_is_a_definite_failure(client):
    t = await _setup()
    h = await login(client, t[1])
    plan = (await _plan(client, h, t[0])).json()
    await _approve_and_queue(client, t, plan)
    w = FakeWriter()
    w.mutate_error = ConnectorError("forbidden", "denied by the API server")
    done = await _run(w)
    assert done.status == "failed" and done.mutation_state == "not_applied"


async def test_a_person_closes_reconciliation_with_an_audit_trail(client):
    t = await _setup()
    pid = await _claimed(client, t)
    await _expire(pid, "intent")
    await ex.reconcile_stale()
    h = await login(client, t[2])
    url = f"/moks/api/actions/plans/{pid}/reconcile"
    assert (await client.post(url, json={"outcome": "applied", "comment": " "}, headers=h)).status_code == 422
    r = await client.post(url, json={"outcome": "not_applied", "comment": "replicas still 2 in kubectl"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "reconciled"
    assert (await client.post(url, json={"outcome": "applied", "comment": "again"}, headers=h)).status_code == 409
    h = await login(client, t[1])
    assert (await client.post(f"/moks/api/actions/plans/{pid}/reconcile", json={"outcome": "applied", "comment": "x"}, headers=h)).status_code == 403


# ---- 6. Validation -----------------------------------------------------------------------------

async def test_hpa_change_is_read_back(client):
    t = await _setup()
    h = await login(client, t[1])
    plan = (await _plan(client, h, t[0], "apply_config_patch", {"patch": {"spec": {"maxReplicas": 5}}}, kind="HorizontalPodAutoscaler")).json()
    assert plan["status"] == "pending"
    await _approve_and_queue(client, t, plan)
    done = await _run(FakeWriter())
    assert done.status == "succeeded" and done.result["validation"]["read_back"], done.result


async def test_hpa_that_loses_scaling_fails_validation_and_recovers(client):
    t = await _setup()
    h = await login(client, t[1])
    plan = (await _plan(client, h, t[0], "apply_config_patch", {"patch": {"spec": {"maxReplicas": 5}}}, kind="HorizontalPodAutoscaler")).json()
    await _approve_and_queue(client, t, plan)
    w = FakeWriter()

    def lose():
        OBJECTS[("shop", "HorizontalPodAutoscaler", "checkout")]["status"]["conditions"] = [{"type": "AbleToScale", "status": "False"}]
    w.after_mutation = lose
    done = await _run(w)
    assert done.result["validation"]["ok"] is False and "AbleToScale" in done.result["validation"]["reason"]
    assert done.status == "failed"  # the condition stayed false after the rollback, so it is not "recovered"
    assert OBJECTS[("shop", "HorizontalPodAutoscaler", "checkout")]["spec"]["maxReplicas"] == 4


async def test_old_restarts_are_not_blamed_on_this_change_but_new_ones_are(client):
    pods = [{"name": "a", "phase": "Running", "restarts": 5, "problems": []}]
    w = FakeWriter()
    w.pod_rows = pods
    assert (await ex.validate_deployment(w, "shop", "checkout", None, 1, {"a": 5}))["ok"]
    pods[0]["restarts"] = 6
    v = await ex.validate_deployment(w, "shop", "checkout", None, 1, {"a": 5})
    assert not v["ok"] and "restarted" in v["reason"]


async def test_service_without_endpoints_fails_a_deployment_validation(client):
    w = FakeWriter("no_endpoints")
    w.calls.append(("x",))
    DEPLOYMENTS[("shop", "checkout")]["template"]["metadata"]["labels"] = {"app": "checkout-v2"}
    v = await ex.validate_deployment(w, "shop", "checkout", None, 1)
    assert not v["ok"] and "no ready endpoints" in v["reason"]


async def test_approval_row_is_still_required(client):
    t = await _setup(prod=True)
    h = await login(client, t[1])
    plan = (await _plan(client, h, t[0])).json()
    await _approve_and_queue(client, t, plan)
    async with session_scope() as db:
        for a in (await db.execute(select(Approval).where(Approval.plan_id == uuid.UUID(plan["id"])))).scalars():
            await db.delete(a)
    done = await _run(FakeWriter())
    assert done.status == "failed" and "no valid approval" in done.result["error"]
