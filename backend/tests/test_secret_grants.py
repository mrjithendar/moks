"""Secret sync under the per-component database roles: the whole flow works with API requests as moks_api and
jobs as moks_executor, and each role is refused what belongs to the other."""
import uuid

import pytest
from sqlalchemy import text

import tests.test_secret_sync as flow
from moks.db import session_scope
from moks.secrets.content import Content, to_aws
from tests.test_db_grants import refused, role, roles  # noqa: F401
from tests.test_secret_sync import World, world  # noqa: F401

pytestmark = pytest.mark.usefixtures("roles")


@pytest.fixture
def as_components(monkeypatch):
    call, run = World.call, flow.run_jobs

    async def api_call(self, *a, **kw):
        token = role.set("moks_api")
        try:
            return await call(self, *a, **kw)
        finally:
            role.reset(token)

    async def executor_jobs(concurrently=False):
        token = role.set("moks_executor")
        try:
            await run(concurrently)
        finally:
            role.reset(token)

    monkeypatch.setattr(World, "call", api_call)
    monkeypatch.setattr(flow, "run_jobs", executor_jobs)


async def test_export_import_and_retry_run_under_the_component_roles(world, as_components):  # noqa: F811
    w = world
    m, done = await flow._export_one(w, {"p": b"one", "e": b""}, direction="both")
    assert done["status"] == "succeeded", done
    w.sm.external_put(m["aws_secret_name"], to_aws(Content("Opaque", {"p": b"two"}), "moks-k8s-v1", {}).string)
    p = await w.plan([m["id"]])
    w.east.after["replace"] = __import__("moks.connectors.base", fromlist=["ConnectorError"]).ConnectorError("timeout", "lost")
    first = await w.approve_and_run(p)
    assert first["items"][0]["status"] == "failed"
    await w.call("op", "POST", f"/plans/{p['id']}/retry", {}, expect=202)
    await flow.run_jobs()
    assert (await w.get(p["id"]))["status"] == "succeeded"
    assert w.east.data("shop", "db") == {"p": b"two"}


async def test_each_role_is_refused_the_others_part(world, as_components):  # noqa: F811
    w = world
    m, done = await flow._export_one(w, {"p": b"one"}, direction="export")
    plan_id, item_id = uuid.UUID(done["id"]), uuid.UUID(done["items"][0]["id"])
    for col in ("write_state = '{}'", "result = '{}'", "attempts = 9", "kube_state = '{}'", "idempotency_token = gen_random_uuid()"):
        await refused("moks_api", f"UPDATE secret_plan_items SET {col} WHERE id = :i", i=item_id)
    for col in ("lease_owner = gen_random_uuid()", "last_status = 'in_sync'", "aws_secret_arn = 'x'"):
        await refused("moks_api", f"UPDATE secret_mappings SET {col} WHERE id = :i", i=uuid.UUID(m["id"]))
    await refused("moks_api", "DELETE FROM secret_plans WHERE id = :i", i=plan_id)
    await refused("moks_api", "DELETE FROM secret_inventory")
    for r in ("moks_api", "moks_executor", "moks_worker", "moks_backup", "moks_terminal"):
        await refused(r, "UPDATE secret_plan_approvals SET decision = 'approve'")
        await refused(r, "DELETE FROM secret_plan_approvals")
    await refused("moks_executor", "INSERT INTO secret_plan_approvals (id, plan_id, approver_id, decision, plan_hash) SELECT gen_random_uuid(), :p, requested_by, 'approve', plan_hash FROM secret_plans WHERE id = :p", p=plan_id)
    await refused("moks_executor", "UPDATE secret_plans SET plan_hash = 'x', requires_approval = false WHERE id = :p", p=plan_id)
    await refused("moks_executor", "UPDATE secret_mappings SET direction = 'both' WHERE id = :i", i=uuid.UUID(m["id"]))
    for t in ("secret_mappings", "secret_plan_items", "secret_inventory", "secret_plans"):
        await refused("moks_worker", f"SELECT * FROM {t}")
        await refused("moks_terminal", f"SELECT * FROM {t}")
    async with session_scope() as db:  # sanity: the row the refusals aimed at exists
        assert (await db.execute(text("SELECT count(*) FROM secret_plan_items WHERE id = :i"), {"i": item_id})).scalar_one() == 1


async def test_schedules_and_reveals_run_under_the_component_roles_and_only_the_api_approves(world, as_components, monkeypatch):  # noqa: F811
    from moks.secrets.seal import open_sealed

    monkeypatch.setattr(flow.get_settings(), "secrets_sync_policy", "scheduled")  # the default, manual, never runs a schedule
    w = world
    m, _ = await flow._export_one(w, {"p": b"one"}, direction="export")
    s = (await w.call("op", "POST", "/schedules", {"name": "roles", "mapping_ids": [m["id"]], "interval_minutes": 30}, expect=201)).json()
    await refused("moks_executor", "INSERT INTO secret_schedule_approvals (id, schedule_id, approver_id, decision, schedule_hash) "
                                   "SELECT gen_random_uuid(), id, requested_by, 'approve', schedule_hash FROM secret_sync_schedules WHERE id = :s", s=uuid.UUID(s["id"]))
    await refused("moks_executor", "UPDATE secret_sync_schedules SET schedule_hash = 'x', mapping_versions = '{}' WHERE id = :s", s=uuid.UUID(s["id"]))
    await refused("moks_worker", "SELECT * FROM secret_sync_schedules")
    await w.call("ap", "POST", f"/schedules/{s['id']}/decision", {"approve": True, "schedule_hash": s["schedule_hash"]}, expect=200)
    w.east.put("shop", "db", {"p": b"two"})
    token = role.set("moks_executor")
    try:
        (run,) = await flow.run_schedules()
    finally:
        role.reset(token)
    assert run["status"] == "succeeded", run
    assert w.aws_content(m["aws_secret_name"]).data == {"p": b"two"}

    # the manual-only policy: the executor holds the schedule, within its column grants
    monkeypatch.setattr(flow.get_settings(), "secrets_sync_policy", "manual")
    s_ = flow.get_settings()
    old_role, s_.process_role = s_.process_role, "executor"
    token = role.set("moks_executor")
    try:
        assert await flow.tasks.hold_schedules() == 1
    finally:
        role.reset(token)
        s_.process_role = old_role
    async with session_scope() as db:
        assert (await db.execute(text("SELECT status FROM secret_sync_schedules WHERE id = :s"), {"s": uuid.UUID(s["id"])})).scalar_one() == "held"
        await db.execute(text("UPDATE secret_sync_schedules SET status = 'retired' WHERE id = :s"), {"s": uuid.UUID(s["id"])})

    rid = next(r["id"] for r in (await w.call("op", "GET", "/inventory?side=kubernetes", expect=200)).json()["items"] if r["name"] == "db")
    private, jwk = flow._browser_key()
    job = (await w.call("ap", "POST", "/reveal", {"inventory_id": rid, "public_key": jwk}, expect=202)).json()["job_id"]
    await flow.run_jobs()
    sealed = (await w.call("ap", "GET", f"/reveal/{job}", expect=200)).json()["sealed"]
    assert __import__("json").loads(open_sealed(private, sealed, job))["keys"] == {"p": {"text": "two"}}
