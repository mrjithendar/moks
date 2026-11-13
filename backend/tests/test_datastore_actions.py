"""Approval-gated data-store actions: cancel a query, end a session, close a Redis client.

PostgreSQL runs for real: a monitoring role plans, an operator role holding only pg_signal_backend executes, and the
victim sessions belong to an ordinary role. Redis runs against the in-process RESP server from test_datastores.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import update

from moks.actions import datastore as ds_actions
from moks.actions import executor as ex
from moks.connectors import registry
from moks.connectors.base import ConnectorError
from moks.connectors.redis import RedisConnector
from moks.db import session_scope
from moks.models import ActionPlan
from tests.conftest import ADMIN_URL, login, make_env, make_user
from tests.test_datastores import INFO, FakeRedisServer


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(ds_actions, "POLL", 0.05)
    monkeypatch.setattr(ex, "VALIDATION_TIMEOUT", 5)
    registry._cache.clear()
    yield
    registry._cache.clear()


async def _admin_conn():
    import asyncpg

    return await asyncpg.connect(ADMIN_URL)


def _hostport():
    host, port = ADMIN_URL.split("@", 1)[1].split("/", 1)[0].split(":")
    return host, int(port)


@pytest.fixture
async def roles(monkeypatch):
    tag = uuid.uuid4().hex[:6]
    names = {"monitor": f"mon_{tag}", "operator": f"op_{tag}", "app": f"app_{tag}"}
    conn = await _admin_conn()
    await conn.execute(f"CREATE ROLE {names['monitor']} LOGIN PASSWORD 'mon' IN ROLE pg_monitor")
    await conn.execute(f"CREATE ROLE {names['operator']} LOGIN PASSWORD 'op' IN ROLE pg_signal_backend, pg_monitor")
    await conn.execute(f"CREATE ROLE {names['app']} LOGIN PASSWORD 'app'")
    await conn.close()
    monkeypatch.setenv("MOKS_CRED_PG_MON", "mon")
    monkeypatch.setenv("MOKS_CRED_PG_OP", "op")
    yield names
    conn = await _admin_conn()
    for r in names.values():
        await conn.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{r}'")
        await conn.execute(f"DROP ROLE IF EXISTS {r}")
    await conn.close()


async def _victim(role: str):
    import asyncpg

    host, port = _hostport()
    return await asyncpg.connect(host=host, port=port, user=role, password="app", database="postgres")


async def _env(roles, *, executor=True, namespace="shop"):
    host, port = _hostport()
    cfg = {"host": host, "port": port, "database": "postgres", "username": roles["monitor"], "sslmode": "disable", "namespace": namespace}
    if executor:
        cfg.update(executor={"username": roles["operator"]}, executor_credential_ref={"type": "env", "name": "MOKS_CRED_PG_OP"})
    name = f"dsa-{uuid.uuid4().hex[:6]}"
    await make_env(name, connectors=[{"type": "postgres", "name": "shop-db", "config": cfg, "credential_ref": {"type": "env", "name": "MOKS_CRED_PG_MON"}}])
    return name


async def _backend_start(pid: int) -> str:
    conn = await _admin_conn()
    started = await conn.fetchval("SELECT backend_start FROM pg_stat_activity WHERE pid = $1", pid)
    await conn.close()
    return started.isoformat()


def _body(env, action, params, namespace="shop", kind="Postgres", name="shop-db"):
    return {"environment": env, "namespace": namespace, "action": action, "target_kind": kind, "target_name": name, "params": params,
            "reason": "holds the accounts lock", "auto_recover": False}


async def _execute(client, h, plan) -> ActionPlan:
    q = await client.post(f"/moks/api/actions/plans/{plan['id']}/execute", json={"plan_hash": plan["plan_hash"]}, headers=h)
    assert q.status_code == 200 and q.json()["status"] == "queued", q.text
    claim = await ex.claim_plan("test-exec")
    assert claim and str(claim.plan_id) == plan["id"]
    await ex.execute_plan(claim)
    async with session_scope() as db:
        return await db.get(ActionPlan, uuid.UUID(plan["id"]))


async def test_terminate_ends_exactly_the_reviewed_session(client, roles):
    env = await _env(roles)
    victim, bystander = await _victim(roles["app"]), await _victim(roles["app"])
    await victim.execute("BEGIN")
    await victim.execute("SELECT 1")  # idle in transaction
    pid = victim.get_server_pid()
    started = await _backend_start(pid)
    h = await login(client, "admin@example.com")

    stale = await client.post("/moks/api/actions/plans", headers=h, json=_body(env, "pg_terminate_session", {"pid": pid, "backend_start": "2001-01-01T00:00:00+00:00"}))
    assert stale.status_code == 409 and "different session" in stale.text  # a pid alone is never enough
    wrong_ns = await client.post("/moks/api/actions/plans", headers=h, json=_body(env, "pg_terminate_session", {"pid": pid, "backend_start": started}, namespace="other"))
    assert wrong_ns.status_code == 403 and "belongs to namespace shop" in wrong_ns.text

    r = await client.post("/moks/api/actions/plans", headers=h, json=_body(env, "pg_terminate_session", {"pid": pid, "backend_start": started}))
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["pre_state"]["state"] == "idle in transaction" and plan["pre_state"]["user"] == roles["app"] and plan["pre_state"]["server"]
    assert "rolled back" in plan["expected_impact"] and plan["auto_recover"] is False
    done = await _execute(client, h, plan)
    assert done.status == "succeeded", done.result
    assert done.result["steps"][0]["signalled"] and done.result["validation"]["gone"]
    with pytest.raises(Exception):
        await asyncio.wait_for(victim.fetchval("SELECT 1"), 5)
    assert await bystander.fetchval("SELECT 1") == 1  # the other session of the same role is untouched
    await bystander.close()


async def test_cancel_stops_the_query_and_keeps_the_session(client, roles):
    import asyncpg

    env = await _env(roles)
    victim = await _victim(roles["app"])
    pid = victim.get_server_pid()
    started = await _backend_start(pid)
    sleeping = asyncio.create_task(victim.fetchval("SELECT pg_sleep(30)"))
    await asyncio.sleep(0.5)
    tag = uuid.uuid4().hex[:6]
    op = f"dsop-{tag}@example.com"
    await make_user(op, [f"operator:{env}:shop"])
    h = await login(client, op)
    r = await client.post("/moks/api/actions/plans", headers=h, json=_body(env, "pg_cancel_query", {"pid": pid, "backend_start": started}))
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["requires_approval"] is False  # cancelling outside production needs no second person
    done = await _execute(client, h, plan)
    assert done.status == "succeeded", done.result
    with pytest.raises(asyncpg.QueryCanceledError):
        await asyncio.wait_for(sleeping, 10)
    assert await victim.fetchval("SELECT 2") == 2
    term = await client.post("/moks/api/actions/plans", headers=h, json=_body(env, "pg_terminate_session", {"pid": pid, "backend_start": started}))
    assert term.status_code == 201 and term.json()["requires_approval"] is True  # ending a session always needs approval
    await victim.close()


async def test_refusals_superuser_no_executor_and_a_replaced_server(client, roles):
    h = await login(client, "admin@example.com")
    env = await _env(roles)
    su = await _admin_conn()
    su_pid = su.get_server_pid()
    r = await client.post("/moks/api/actions/plans", headers=h, json=_body(env, "pg_terminate_session", {"pid": su_pid, "backend_start": await _backend_start(su_pid)}))
    assert r.status_code == 403 and "superuser" in r.text
    await su.close()

    readonly_env = await _env(roles, executor=False)
    victim = await _victim(roles["app"])
    pid = victim.get_server_pid()
    started = await _backend_start(pid)
    r = await client.post("/moks/api/actions/plans", headers=h, json=_body(readonly_env, "pg_terminate_session", {"pid": pid, "backend_start": started}))
    assert r.status_code == 409 and "no write target" in r.text

    plan = (await client.post("/moks/api/actions/plans", headers=h, json=_body(env, "pg_terminate_session", {"pid": pid, "backend_start": started}))).json()
    async with session_scope() as db:  # as if the cluster had been rebuilt between review and execution
        await db.execute(update(ActionPlan).where(ActionPlan.id == uuid.UUID(plan["id"])).values(pre_state={**plan["pre_state"], "server": "123"}))
    done = await _execute(client, h, plan)
    assert done.status == "failed" and "not the server this plan was reviewed against" in done.result["error"]
    assert await victim.fetchval("SELECT 1") == 1  # nothing was signalled
    await victim.close()


class KillableRedis(FakeRedisServer):
    def __init__(self):
        super().__init__(info=INFO.replace("# Server\r\n", "# Server\r\nrun_id:abc123\r\n"))
        self.clients = {1: "10.0.0.5:4000", 2: "10.0.0.6:4001"}

    def _answer(self, cmd, authed):
        if authed and cmd[0].upper() == "CLIENT":
            sub = cmd[1].upper()
            if sub == "LIST":
                text = "".join(f"id={i} addr={a} name= age=10 idle=5 flags=N db=0 omem=0 cmd=blpop\n" for i, a in self.clients.items())
                return b"$%d\r\n%s\r\n" % (len(text), text.encode())
            if sub == "KILL":
                gone = self.clients.pop(int(cmd[3]), None)
                return b":%d\r\n" % (1 if gone else 0)
        return super()._answer(cmd, authed)


async def test_redis_client_is_closed_only_by_the_executor(client, monkeypatch, redis_operator_env):
    srv, env = redis_operator_env
    h = await login(client, "admin@example.com")
    bad = await client.post("/moks/api/actions/plans", headers=h, json=_body(env, "redis_kill_client", {"client_id": 2, "addr": "10.9.9.9:1"}, namespace="cache", kind="Redis", name="cache"))
    assert bad.status_code == 409 and "different address" in bad.text
    r = await client.post("/moks/api/actions/plans", headers=h, json=_body(env, "redis_kill_client", {"client_id": 2, "addr": "10.0.0.6:4001"}, namespace="cache", kind="Redis", name="cache"))
    assert r.status_code == 201, r.text
    assert r.json()["pre_state"]["server"] == "abc123" and r.json()["requires_approval"] is False  # an admin's plan
    assert not any(c[:2] == ["CLIENT", "KILL"] for c in srv.commands)  # planning sends nothing that writes
    done = await _execute(client, h, r.json())
    assert done.status == "succeeded", done.result
    assert ["CLIENT", "KILL", "ID", "2"] in srv.commands and 2 not in srv.clients and 1 in srv.clients
    kills = [c for c in srv.commands if c[:2] == ["CLIENT", "KILL"]]
    assert len(kills) == 1


@pytest.fixture
async def redis_operator_env(monkeypatch):
    monkeypatch.setenv("MOKS_CRED_REDIS_MON", "pw")
    monkeypatch.setenv("MOKS_CRED_REDIS_OP", "pw")
    async with KillableRedis() as srv:
        name = f"dsr-{uuid.uuid4().hex[:6]}"
        await make_env(name, connectors=[{"type": "redis", "name": "cache", "credential_ref": {"type": "env", "name": "MOKS_CRED_REDIS_MON"},
                                          "config": {"host": "127.0.0.1", "port": srv.port, "namespace": "cache", "username": "moks",
                                                     "executor": {"username": "moks-operator"}, "executor_credential_ref": {"type": "env", "name": "MOKS_CRED_REDIS_OP"}}}])
        yield srv, name
        registry._cache.clear()


async def test_monitoring_connector_cannot_kill(redis_operator_env):
    srv, _ = redis_operator_env
    r = RedisConnector({"host": "127.0.0.1", "port": srv.port}, {"type": "env", "name": "MOKS_CRED_REDIS_MON"})
    with pytest.raises(ConnectorError) as e:
        await r.kill_client(1)
    assert e.value.kind == "forbidden"
    with pytest.raises(ConnectorError):
        await r._call("x", "CLIENT", "KILL", "ID", "1")
    await r.aclose()
    assert not any(c[:2] == ["CLIENT", "KILL"] for c in srv.commands)
