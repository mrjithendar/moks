"""Losing access takes effect during running work, not at the next investigation."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select, update

from moks import conversations
from moks.auth import revocation
from moks.auth.revocation import AccessGuard, AccessRevoked
from moks.connectors import registry
from moks.db import session_scope
from moks.models import Environment, Investigation, RoleBinding, User
from moks.orchestrator import engine
from moks.orchestrator.engine import Investigator
from moks.rbac import P
from tests.conftest import login, make_env, make_user
from tests.fakes import FakeKubernetes


@pytest.fixture(autouse=True)
def k8s(monkeypatch):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "kubernetes", FakeKubernetes)
    registry._cache.clear()
    yield
    registry._cache.clear()


async def _world():
    name = f"rev-{uuid.uuid4().hex[:6]}"
    env_id = await make_env(name, connectors=[{"type": "kubernetes", "name": "k8s", "config": {"mode": "in_cluster"}}])
    email = f"rev-{uuid.uuid4().hex[:6]}@example.com"
    uid = await make_user(email, [f"investigator:{name}:*"])
    return name, env_id, email, uid


async def _guard(uid, env_id, ns="shop"):
    g = AccessGuard(uid, env_id, ns, P.INVESTIGATION_RUN)
    await g.check()
    return g


async def test_a_removed_role_is_noticed():
    _, env_id, _, uid = await _world()
    g = await _guard(uid, env_id)
    async with session_scope() as db:
        await db.execute(delete(RoleBinding).where(RoleBinding.user_id == uid))
    with pytest.raises(AccessRevoked):
        await g.check()


async def test_a_disabled_account_is_noticed():
    _, env_id, _, uid = await _world()
    g = await _guard(uid, env_id)
    async with session_scope() as db:
        await db.execute(update(User).where(User.id == uid).values(is_active=False))
    with pytest.raises(AccessRevoked):
        await g.check()


async def test_a_namespace_denied_by_policy_is_noticed():
    _, env_id, _, uid = await _world()
    g = await _guard(uid, env_id)
    async with session_scope() as db:
        await db.execute(update(Environment).where(Environment.id == env_id).values(denied_namespaces=["shop"]))
    with pytest.raises(AccessRevoked):
        await g.check()


async def test_versions_move_only_on_relevant_changes():
    _, env_id, _, uid = await _world()
    async def version():
        async with session_scope() as db:
            return (await db.get(User, uid)).auth_version, (await db.get(Environment, env_id)).policy_version
    start = await version()
    async with session_scope() as db:
        await db.execute(update(User).where(User.id == uid).values(display_name="renamed"))
    assert await version() == start
    async with session_scope() as db:
        db.add(RoleBinding(user_id=uid, role="viewer", environment="*", namespace="*"))
    assert (await version())[0] == start[0] + 1


async def test_unchanged_versions_do_not_reload_roles(monkeypatch):
    _, env_id, _, uid = await _world()
    g = await _guard(uid, env_id)
    monkeypatch.setattr(revocation, "effective_scope", None)  # any reload would crash
    await g.check()


async def _running(client, env, email):
    h = await login(client, email)
    r = await client.post("/moks/api/investigations", json={"question": "what broke?", "environment": env, "namespace": "shop"}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"], h


async def test_revocation_stops_a_running_investigation_within_the_delay(client, monkeypatch):
    monkeypatch.setattr(engine, "WATCH_SECONDS", 0.05)
    env, _, email, uid = await _world()
    iid, _ = await _running(client, env, email)
    entered = asyncio.Event()
    real = FakeKubernetes.list_pods

    async def stuck(self, *a, **k):
        entered.set()
        await asyncio.sleep(30)
        return await real(self, *a, **k)
    monkeypatch.setattr(FakeKubernetes, "list_pods", stuck)
    run = asyncio.create_task(Investigator(uuid.UUID(iid), "t").run())
    await asyncio.wait_for(entered.wait(), 5)
    async with session_scope() as db:
        await db.execute(delete(RoleBinding).where(RoleBinding.user_id == uid))
    await asyncio.wait_for(run, 5)
    async with session_scope() as db:
        inv = await db.get(Investigation, uuid.UUID(iid))
    assert inv.status == "cancelled" and "access was revoked" in inv.error and inv.report is None


async def test_no_tool_runs_after_revocation(client, monkeypatch):
    env, env_id, email, uid = await _world()
    iid, _ = await _running(client, env, email)
    real = AccessGuard.check
    calls = {"n": 0}

    async def revoke_after_first(self):
        calls["n"] += 1
        if calls["n"] == 2:
            async with session_scope() as db:
                await db.execute(delete(RoleBinding).where(RoleBinding.user_id == uid))
        await real(self)
    monkeypatch.setattr(AccessGuard, "check", revoke_after_first)
    await Investigator(uuid.UUID(iid), "t").run()
    async with session_scope() as db:
        inv = await db.get(Investigation, uuid.UUID(iid))
    assert inv.status == "failed" and "not permitted" in inv.error


async def test_nothing_is_sent_to_the_llm_after_revocation():
    class Boom:
        cfg = None
        async def complete(self, *a, **k):
            raise AssertionError("the provider must not be called")
    inv = Investigator(uuid.uuid4(), "t")

    class Guard:
        async def check(self):
            raise AccessRevoked("gone")
    inv.guard = Guard()
    with pytest.raises(AccessRevoked):
        await inv._llm(Boom(), None, "s", "u", 10)


async def test_open_stream_ends_when_access_is_lost(client, monkeypatch):
    from moks.api import investigations

    monkeypatch.setattr(investigations, "STREAM_RECHECK_SECONDS", 0.01)
    env, _, email, uid = await _world()
    iid, _ = await _running(client, env, email)
    real = investigations.load

    async def revoked_while_connected(db, cookie):
        async with session_scope() as s:
            await s.execute(delete(RoleBinding).where(RoleBinding.user_id == uid))
        return await real(db, cookie)
    monkeypatch.setattr(investigations, "load", revoked_while_connected)
    async with client.stream("GET", f"/moks/api/investigations/{iid}/events") as r:
        body = await asyncio.wait_for(r.aread(), 5)
    assert b"event: revoked" in body


async def test_open_stream_ends_when_the_session_ends(client, monkeypatch):
    from moks.api import investigations

    monkeypatch.setattr(investigations, "STREAM_RECHECK_SECONDS", 0.01)
    env, _, email, _ = await _world()
    iid, _ = await _running(client, env, email)

    async def signed_out(db, cookie):
        return None
    monkeypatch.setattr(investigations, "load", signed_out)
    async with client.stream("GET", f"/moks/api/investigations/{iid}/events") as r:
        body = await asyncio.wait_for(r.aread(), 5)
    assert b"event: revoked" in body


async def test_history_leaves_out_turns_the_asker_can_no_longer_read():
    from moks.models import Conversation, ConversationMessage
    from moks.rbac import Binding, Principal

    name, env_id, _, uid = await _world()
    now = datetime.now(timezone.utc)
    async with session_scope() as db:
        conv = Conversation(title="t", environment_id=env_id, namespace="shop", created_by=uid)
        db.add(conv)
        await db.flush()
        secret = Investigation(question="q", environment_id=env_id, namespace="secret", created_by=uid, time_from=now - timedelta(minutes=5), time_to=now, status="completed")
        db.add(secret)
        await db.flush()
        db.add(ConversationMessage(conversation_id=conv.id, role="assistant", content="hidden turn", investigation_id=secret.id))
        db.add(ConversationMessage(conversation_id=conv.id, author_id=uid, role="user", content="plain note"))
        await db.flush()
        who = Principal(str(uid), "x", [Binding("viewer", name, "shop")])
        got = await conversations.history(db, conv.id, uuid.uuid4(), who, name)
    texts = [m["text"] for m in got]
    assert "plain note" in texts and "hidden turn" not in texts
    assert select(User) is not None
