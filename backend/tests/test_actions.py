"""Phase 3: action plans, approvals (expiry, replay, parameter changes, self-approval), executor
re-verification, validation and automatic recovery."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from moks.actions import executor as ex
from moks.connectors import registry
from moks.db import session_scope
from moks.models import ActionPlan, AuditEvent, RoleBinding
from tests.conftest import login, make_env, make_user
from tests.fakes import DEPLOYMENTS, FakeKubernetes, FakeWriter, reset_deployments


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "kubernetes", FakeKubernetes)
    registry._cache.clear()
    reset_deployments()
    FakeWriter.instances.clear()
    monkeypatch.setattr(ex, "POLL", 0.01)
    monkeypatch.setattr(ex, "VALIDATION_TIMEOUT", 1)
    yield
    registry._cache.clear()


async def _setup(client, prod=True):
    name = f"act-{uuid.uuid4().hex[:6]}"
    env_id = await make_env(name, connectors=[{"type": "kubernetes", "name": "k8s", "config": {"mode": "in_cluster", "executor": {"mode": "in_cluster"}}}])
    async with session_scope() as db:
        from moks.models import Environment

        e = await db.get(Environment, env_id)
        e.is_production = prod
        e.max_replicas = 5
    tag = uuid.uuid4().hex[:6]
    op, ap = f"op-{tag}@example.com", f"ap-{tag}@example.com"
    await make_user(op, [f"operator:{name}:shop"])
    await make_user(ap, [f"approver:{name}:shop"])
    return name, op, ap


async def _plan(client, h, env, action="scale_deployment", params=None, **kw):
    body = {"environment": env, "namespace": "shop", "action": action, "target_name": "checkout", "params": params if params is not None else {"replicas": 4},
            "reason": "checkout saturated at peak", **kw}
    return await client.post("/moks/api/actions/plans", json=body, headers=h)


async def _run_executor(writer):
    async def factory(env, connector_id=None):
        return writer

    ex.writer_factory = factory
    claim = await ex.claim_plan("test-exec")
    assert claim
    pid = claim.plan_id
    await ex.execute_plan(claim)
    async with session_scope() as db:
        return await db.get(ActionPlan, pid)


async def test_full_flow_with_separation_of_duties(client):
    env, op, ap = await _setup(client)
    h_op = await login(client, op)
    r = await _plan(client, h_op, env)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["requires_approval"] and plan["status"] == "pending" and plan["pre_state"]["resourceVersion"] == "100"
    assert "2 to 4 replicas" in plan["expected_impact"] and plan["rollback"] and plan["validation"] and plan["preconditions"]
    # operator cannot execute before approval, nor approve their own plan
    assert (await client.post(f"/moks/api/actions/plans/{plan['id']}/execute", json={"plan_hash": plan["plan_hash"]}, headers=h_op)).status_code == 409
    h_op2 = await login(client, op)
    assert (await client.post(f"/moks/api/actions/plans/{plan['id']}/approve", json={"plan_hash": plan["plan_hash"]}, headers=h_op2)).status_code == 403
    # approver approves the exact hash they reviewed
    h_ap = await login(client, ap)
    bad = await client.post(f"/moks/api/actions/plans/{plan['id']}/approve", json={"plan_hash": "0" * 64}, headers=h_ap)
    assert bad.status_code == 409
    ok = await client.post(f"/moks/api/actions/plans/{plan['id']}/approve", json={"plan_hash": plan["plan_hash"], "comment": "lgtm"}, headers=h_ap)
    assert ok.json()["status"] == "approved"
    # approver may not execute (no action.execute)
    assert (await client.post(f"/moks/api/actions/plans/{plan['id']}/execute", json={"plan_hash": plan["plan_hash"]}, headers=h_ap)).status_code == 403
    h_op = await login(client, op)
    q = await client.post(f"/moks/api/actions/plans/{plan['id']}/execute", json={"plan_hash": plan["plan_hash"]}, headers=h_op)
    assert q.json()["status"] == "queued"
    # replay: a second execute is refused
    assert (await client.post(f"/moks/api/actions/plans/{plan['id']}/execute", json={"plan_hash": plan["plan_hash"]}, headers=h_op)).status_code == 409
    done = await _run_executor(FakeWriter("ok"))
    assert done.status == "succeeded", done.result
    assert DEPLOYMENTS[("shop", "checkout")]["replicas"] == 4 and done.result["validation"]["ok"]


async def test_validation_failure_triggers_automatic_recovery(client):
    env, op, ap = await _setup(client, prod=False)  # restart needs no approval outside production
    h = await login(client, op)
    plan = (await _plan(client, h, env, action="restart_deployment", params={})).json()
    assert plan["requires_approval"] is False
    assert (await client.post(f"/moks/api/actions/plans/{plan['id']}/execute", json={"plan_hash": plan["plan_hash"]}, headers=h)).json()["status"] == "queued"
    w = FakeWriter("crashloop")
    done = await _run_executor(w)
    assert done.status == "recovered"
    assert "CrashLoopBackOff" in done.result["validation"]["reason"]
    assert done.result["recovery"]["status"] == "applied" and done.result["recovery"]["validation"]["ok"]
    assert "annotations" not in DEPLOYMENTS[("shop", "checkout")]["template"]["metadata"]  # the pre-action template is back


async def test_resource_changed_after_review_is_not_touched(client):
    env, op, ap = await _setup(client, prod=False)
    h = await login(client, op)
    plan = (await _plan(client, h, env, params={"replicas": 3})).json()
    await client.post(f"/moks/api/actions/plans/{plan['id']}/execute", json={"plan_hash": plan["plan_hash"]}, headers=h)
    DEPLOYMENTS[("shop", "checkout")]["resourceVersion"] = "999"  # someone else changed it
    w = FakeWriter("ok")
    done = await _run_executor(w)
    assert done.status == "failed" and "changed after review" in done.result["error"]
    assert not [c for c in w.calls if c[0] == "scale"]


async def test_parameter_change_after_approval_is_rejected_by_executor(client):
    env, op, ap = await _setup(client)
    h_op = await login(client, op)
    plan = (await _plan(client, h_op, env)).json()
    h_ap = await login(client, ap)
    await client.post(f"/moks/api/actions/plans/{plan['id']}/approve", json={"plan_hash": plan["plan_hash"]}, headers=h_ap)
    h_op = await login(client, op)
    await client.post(f"/moks/api/actions/plans/{plan['id']}/execute", json={"plan_hash": plan["plan_hash"]}, headers=h_op)
    async with session_scope() as db:  # tamper with parameters behind the API's back
        await db.execute(update(ActionPlan).where(ActionPlan.id == uuid.UUID(plan["id"])).values(params={"replicas": 5}))
    w = FakeWriter("ok")
    done = await _run_executor(w)
    assert done.status == "failed" and "hash mismatch" in done.result["error"] and not w.calls


async def test_expired_plans_cannot_be_approved(client):
    env, op, ap = await _setup(client)
    h_op = await login(client, op)
    plan = (await _plan(client, h_op, env)).json()
    async with session_scope() as db:
        await db.execute(update(ActionPlan).where(ActionPlan.id == uuid.UUID(plan["id"])).values(
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    h_ap = await login(client, ap)
    r = await client.post(f"/moks/api/actions/plans/{plan['id']}/approve", json={"plan_hash": plan["plan_hash"]}, headers=h_ap)
    assert r.status_code == 409 and "expired" in r.json()["detail"]


async def test_revoked_approver_invalidates_the_approval(client):
    env, op, ap = await _setup(client)
    h_op = await login(client, op)
    plan = (await _plan(client, h_op, env)).json()
    h_ap = await login(client, ap)
    await client.post(f"/moks/api/actions/plans/{plan['id']}/approve", json={"plan_hash": plan["plan_hash"]}, headers=h_ap)
    h_op = await login(client, op)
    await client.post(f"/moks/api/actions/plans/{plan['id']}/execute", json={"plan_hash": plan["plan_hash"]}, headers=h_op)
    async with session_scope() as db:
        from moks.models import User

        uid = (await db.execute(select(User.id).where(User.email == ap))).scalar_one()
        for b in (await db.execute(select(RoleBinding).where(RoleBinding.user_id == uid))).scalars():
            await db.delete(b)
    done = await _run_executor(FakeWriter("ok"))
    assert done.status == "failed" and "no valid approval" in done.result["error"]


async def test_policy_limits_and_forbidden_patch_fields(client):
    env, op, ap = await _setup(client)
    h = await login(client, op)
    assert (await _plan(client, h, env, params={"replicas": 50})).status_code == 403          # above max_replicas
    assert (await _plan(client, h, env, params={"replicas": 0})).status_code == 403           # scale to zero not allowed
    assert (await _plan(client, h, env, action="delete_namespace", params={})).status_code == 400  # not allowlisted
    r = await _plan(client, h, env, action="apply_config_patch", params={"patch": {"spec": {"template": {"spec": {"hostNetwork": True}}}}})
    assert r.status_code == 422 and "hostNetwork" in r.json()["detail"]
    r = await _plan(client, h, env, action="rollback_deployment", params={"to_revision": 9})
    assert r.status_code == 409 and "not in the rollout history" in r.json()["detail"]
    other_ns = await client.post("/moks/api/actions/plans", json={"environment": env, "namespace": "secure", "action": "restart_deployment",
                                                                   "target_name": "vault", "params": {}, "reason": "try other ns"}, headers=h)
    assert other_ns.status_code == 403
    async with session_scope() as db:
        denials = (await db.execute(select(AuditEvent).where(AuditEvent.action == "action.plan"))).scalars().all()
    assert all(d.decision == "allow" for d in denials)  # refused plans never reached the audit as created


def test_writer_exists_only_in_the_executor_process():
    from moks.actions.writer import KubernetesWriter
    from moks.connectors.base import ConnectorError

    with pytest.raises(ConnectorError):
        KubernetesWriter({"mode": "in_cluster"})
