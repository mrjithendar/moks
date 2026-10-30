"""Per-component database roles: real flows work under each role, and everything else is refused."""
import contextvars
import uuid

import asyncpg
import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from moks import db_roles
from moks.actions import executor as ex
from moks.db import engine, session_scope
from moks.models import ActionPlan, Base
from tests.conftest import ADMIN_URL, DB_NAME, login
from tests.fakes import FakeWriter
from tests.test_action_safety import _get, _plan, _setup, fakes  # noqa: F401

role: contextvars.ContextVar[str | None] = contextvars.ContextVar("role", default=None)


@pytest.fixture(autouse=True)
async def roles():
    conn = await asyncpg.connect(ADMIN_URL.rsplit("/", 1)[0] + f"/{DB_NAME}")
    await db_roles.apply(conn, DB_NAME)
    await conn.close()

    def set_role(c):
        if (r := role.get()):
            c.exec_driver_sql(f"SET LOCAL ROLE {r}")
    event.listen(engine().sync_engine, "begin", set_role)
    yield
    event.remove(engine().sync_engine, "begin", set_role)


def test_every_table_is_classified():
    assert set(Base.metadata.tables) <= set(db_roles.TABLES)


async def refused(r: str, sql: str, **params):
    token = role.set(r)
    try:
        with pytest.raises(DBAPIError) as e:
            async with session_scope() as db:
                await db.execute(text(sql), params)
        assert any(w in str(e.value) for w in ("permission denied", "may not move", "must be owner"))
    finally:
        role.reset(token)


async def _plan_row(client, status="pending"):
    t = await _setup()
    h = await login(client, t[1])
    plan = (await _plan(client, h, t[0])).json()
    if status != "pending":
        async with session_scope() as db:
            await db.execute(text("UPDATE action_plans SET status = :s WHERE id = :i"), {"s": status, "i": plan["id"]})
    return plan


@pytest.mark.parametrize("r", ["moks_worker", "moks_backup"])
async def test_only_api_and_executor_touch_plans(client, r):
    plan = await _plan_row(client)
    await refused(r, "UPDATE action_plans SET status = 'approved' WHERE id = :i", i=plan["id"])


async def test_api_cannot_write_execution_state(client):
    plan = await _plan_row(client)
    for col in ("mutation_state = 'applied'", "lease_expires_at = now()", "execution_token = gen_random_uuid()", "checkpoint = '{}'"):
        await refused("moks_api", f"UPDATE action_plans SET {col} WHERE id = :i", i=plan["id"])


@pytest.mark.parametrize("old,new", [("pending", "succeeded"), ("pending", "executing"), ("queued", "executing"), ("executing", "failed"), ("failed", "succeeded")])
async def test_api_cannot_force_an_outcome(client, old, new):
    plan = await _plan_row(client, old)
    await refused("moks_api", f"UPDATE action_plans SET status = '{new}' WHERE id = :i", i=plan["id"])


async def test_approvals_cannot_be_rewritten(client):
    for r in db_roles.ROLES:
        await refused(r, "UPDATE approvals SET decision = 'approve'")
        await refused(r, "DELETE FROM approvals")


async def test_audit_cannot_be_rewritten():
    for r in db_roles.ROLES:
        await refused(r, "UPDATE audit_events SET action = 'x'")


async def test_workers_cannot_change_access_or_read_sessions():
    await refused("moks_worker", "UPDATE role_bindings SET role = 'admin'")
    await refused("moks_worker", "SELECT * FROM sessions")
    await refused("moks_executor", "UPDATE users SET is_active = true")
    await refused("moks_backup", "UPDATE users SET is_active = true")


async def test_no_runtime_role_can_change_schema():
    for r in db_roles.ROLES:
        await refused(r, f"CREATE TABLE t{uuid.uuid4().hex} (id int)")
        await refused(r, "ALTER TABLE users ADD COLUMN x int")


async def test_a_whole_plan_runs_under_the_right_roles(client):
    t = await _setup(prod=True)
    api = role.set("moks_api")
    h = await login(client, t[1])
    plan = (await _plan(client, h, t[0])).json()
    assert plan.get("id"), plan
    ha = await login(client, t[2])
    r = await client.post(f"/moks/api/actions/plans/{plan['id']}/approve", json={"plan_hash": plan["plan_hash"]}, headers=ha)
    assert r.status_code == 200, r.text
    h = await login(client, t[1])
    r = await client.post(f"/moks/api/actions/plans/{plan['id']}/execute", json={"plan_hash": plan["plan_hash"]}, headers=h)
    assert r.status_code == 200, r.text
    role.reset(api)
    token = role.set("moks_executor")
    writer = FakeWriter()

    async def factory(env, connector_id=None):
        return writer
    ex.writer_factory = factory
    claim = await ex.claim_plan("role-exec")
    await ex.execute_plan(claim)
    role.reset(token)
    done = await _get(claim.plan_id)
    assert done.status == "succeeded", done.result
    assert isinstance(done, ActionPlan)


async def test_an_investigation_runs_under_the_worker_role(client):
    from moks.orchestrator.engine import Investigator

    t = await _setup()
    from tests.conftest import make_user
    who = f"inv-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(who, [f"investigator:{t[0]}:*"])
    api = role.set("moks_api")
    h = await login(client, who)
    r = await client.post("/moks/api/investigations", json={"question": "why is checkout down", "environment": t[0], "namespace": "shop"}, headers=h)
    assert r.status_code == 201, r.text
    role.reset(api)
    token = role.set("moks_worker")
    await Investigator(uuid.UUID(r.json()["id"]), "role-worker").run()
    role.reset(token)
    async with session_scope() as db:
        assert (await db.execute(text("SELECT status FROM investigations WHERE id = :i"), {"i": r.json()["id"]})).scalar() == "completed"
