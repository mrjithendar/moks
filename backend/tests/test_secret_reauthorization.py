"""Review regressions for secret sync: reveals are reauthorized before the executor reads and before the API hands
the sealed result over; a running plan is reauthorized before every item and right before every write; the
manual-only policy holds schedules. Same harness as test_secret_sync.py (real API, queue and executor code, fake
Kubernetes and Secrets Manager).
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from moks.config import get_settings
from moks.db import session_scope
from moks.secrets import tasks
from tests.conftest import make_user
from tests.test_secret_sync import SENTINEL, World, _browser_key, _export_one, _inventory, run_jobs, run_schedules, world  # noqa: F401


async def _grant(w: World, who: str, grants: list[str]) -> None:
    email = f"{who}-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(email, grants)
    w.users[who] = email


async def _revoke(w: World, who: str, namespace: str | None = None) -> None:
    async with session_scope() as db:
        await db.execute(text("DELETE FROM role_bindings WHERE user_id = (SELECT id FROM users WHERE email = :e) AND (CAST(:ns AS text) IS NULL OR namespace = :ns)"),
                         {"e": w.users[who], "ns": namespace})


async def _restore(w: World, who: str, role: str, namespace: str = "*") -> None:
    async with session_scope() as db:
        await db.execute(text("""INSERT INTO role_bindings (id, user_id, role, environment, namespace, source)
                                 SELECT gen_random_uuid(), id, :r, :env, :ns, 'manual' FROM users WHERE email = :e"""),
                         {"e": w.users[who], "r": role, "env": w.env, "ns": namespace})


async def _job(job_id: str) -> tuple[str, dict | None, str | None]:
    async with session_scope() as db:
        row = (await db.execute(text("SELECT status, result, error FROM jobs WHERE id = :j"), {"j": uuid.UUID(job_id)})).one()
    return row[0], row[1], row[2]


async def _audit(action: str, decision: str | None = None) -> list[dict]:
    async with session_scope() as db:
        rows = (await db.execute(text("SELECT decision, details FROM audit_events WHERE action = :a"), {"a": action})).all()
    return [{"decision": d, "details": x} for d, x in rows if decision is None or d == decision]


# ---- reveals --------------------------------------------------------------------------------------------------


@pytest.fixture
async def two_namespaces(world):
    """rv may view secrets in shop (A) and pay (B), through two separate bindings."""
    w = world
    await _grant(w, "rv", [f"secrets_approver:{w.env}:shop", f"secrets_approver:{w.env}:pay"])
    w.east.put("shop", "db", {"password": SENTINEL.encode()})
    w.east.put("pay", "card", {"token": b"pay-token"})
    await w.discover(namespaces=("shop", "pay"))
    inv = {(r["namespace"], r["name"]): r["id"] for r in await _inventory(w)}
    return w, inv


async def test_revoking_a_namespace_before_the_executor_reads_denies_the_reveal(two_namespaces):
    w, inv = two_namespaces
    _, jwk = _browser_key()
    job = (await w.call("rv", "POST", "/reveal", {"inventory_id": inv[("shop", "db")], "public_key": jwk}, expect=202)).json()["job_id"]
    await _revoke(w, "rv", "shop")  # loses A, keeps B
    await run_jobs()
    status, result, error = await _job(job)
    assert status == "failed" and "no longer view secrets" in error and not result
    assert SENTINEL not in json.dumps([status, result, error])
    assert (await w.call("rv", "GET", f"/reveal/{job}", expect=200)).json()["status"] == "failed"
    assert any(a["details"].get("reason") for a in await _audit("secrets.reveal", "deny"))
    # B still works for the same person
    job_b = (await w.call("rv", "POST", "/reveal", {"inventory_id": inv[("pay", "card")], "public_key": jwk}, expect=202)).json()["job_id"]
    await run_jobs()
    assert "sealed" in (await w.call("rv", "GET", f"/reveal/{job_b}", expect=200)).json()


async def test_revoking_a_namespace_after_execution_withholds_and_removes_the_sealed_result(two_namespaces):
    w, inv = two_namespaces
    _, jwk = _browser_key()
    job = (await w.call("rv", "POST", "/reveal", {"inventory_id": inv[("shop", "db")], "public_key": jwk}, expect=202)).json()["job_id"]
    await run_jobs()
    assert "sealed" in ((await _job(job))[1] or {})
    await _revoke(w, "rv", "shop")
    r = await w.call("rv", "GET", f"/reveal/{job}")
    assert r.status_code == 403 and "sealed" not in r.text
    _, result, _ = await _job(job)
    assert result == {"withheld": True, "key_count": 1}  # removed, not left for later
    assert (await w.call("rv", "GET", f"/reveal/{job}")).status_code == 410
    await _restore(w, "rv", "secrets_approver", "shop")  # getting access back does not bring an undelivered result back
    assert (await w.call("rv", "GET", f"/reveal/{job}")).status_code == 410
    assert await _audit("secrets.reveal_withheld", "deny")


async def test_a_reveal_is_bound_to_the_connector_and_mapping_it_was_asked_under(two_namespaces):
    w, inv = two_namespaces
    _, jwk = _browser_key()
    job = (await w.call("rv", "POST", "/reveal", {"inventory_id": inv[("shop", "db")], "public_key": jwk}, expect=202)).json()["job_id"]
    async with session_scope() as db:  # the connector is pointed elsewhere before the executor runs
        await db.execute(text("UPDATE connectors SET config = jsonb_set(config, '{executor,context}', '\"other\"') WHERE id = :i"), {"i": uuid.UUID(w.ids["east"])})
    await run_jobs()
    status, _, error = await _job(job)
    assert status == "failed" and "reconfigured" in error
    # a reveal queued before this version (no binding) is never served
    async with session_scope() as db:
        await db.execute(text("UPDATE jobs SET payload = payload - 'binding', status = 'succeeded', result = '{\"sealed\": \"x\", \"key_count\": 1}' WHERE id = :j"), {"j": uuid.UUID(job)})
    assert (await w.call("rv", "GET", f"/reveal/{job}")).status_code == 403
    assert (await _job(job))[1] == {"withheld": True, "key_count": 1}


# ---- bulk execution -------------------------------------------------------------------------------------------


async def _three(w: World) -> tuple[list[dict], dict]:
    for n in ("a", "b", "c"):
        w.east.put("shop", n, {"p": n.encode()})
    await w.discover()
    ms = [await w.mapping(n, direction="export") for n in ("a", "b", "c")]
    return ms, await w.plan([m["id"] for m in ms])


async def _change(w: World, how: str, plan_id: str) -> None:
    async with session_scope() as db:
        if how == "revoke":
            await db.execute(text("DELETE FROM role_bindings WHERE user_id = (SELECT id FROM users WHERE email = :e)"), {"e": w.users["op"]})
        elif how == "expire":
            await db.execute(text("UPDATE secret_plans SET expires_at = now() - interval '1 minute' WHERE id = :p"), {"p": uuid.UUID(plan_id)})
        elif how == "disable":
            await db.execute(text("UPDATE connectors SET enabled = false WHERE id = :i"), {"i": uuid.UUID(w.ids["aws"])})
        elif how == "retarget":
            await db.execute(text("UPDATE connectors SET config = jsonb_set(config, '{allowed_prefixes}', '[\"other/\"]') WHERE id = :i"), {"i": uuid.UUID(w.ids["aws"])})


AUTHORIZE = tasks.authorize_item


def _pause_after(monkeypatch, n: int, action) -> None:
    """Runs `action` once, right after the n-th authorization check of the plan returns."""
    orig, seen = tasks.authorize_item, {"n": 0}

    async def wrapped(*a, **k):
        out = await orig(*a, **k)
        seen["n"] += 1
        if seen["n"] == n:
            await action()
        return out

    monkeypatch.setattr(tasks, "authorize_item", wrapped)


@pytest.mark.parametrize("how,stale", [("revoke", False), ("expire", False), ("disable", True), ("retarget", True)])
async def test_a_change_after_the_first_item_stops_every_later_write(world, monkeypatch, how, stale):
    w = world
    ms, p = await _three(w)
    # checks: a starts (1), before a's write (2), b starts (3), before b's write (4): the change lands between 3 and 4
    _pause_after(monkeypatch, 3, lambda: _change(w, how, p["id"]))
    await w.approve_and_run(p, run=False)
    await run_jobs()
    done = await w.get(p["id"], "ap")  # op may have lost access
    by_name = {i["mapping"]["secret_name"]: i for i in done["items"]}
    assert by_name["a"]["status"] == "succeeded"
    assert {by_name[n]["status"] for n in ("b", "c")} == {"stale" if stale else "failed"}
    assert "stopped before writing" in by_name["b"]["error"] and by_name["c"]["error"].startswith("not run")
    assert set(w.sm.secrets) == {ms[0]["aws_secret_name"]}  # nothing for b or c reached AWS
    halted = done["summary"]["halted"]
    assert halted["completed"] == [by_name["a"]["id"]] and halted["not_run"] == [by_name["c"]["id"]] and halted["stale"] is stale and not halted["ambiguous"]
    assert done["status"] == "partial" and "stopped before finishing" in done["error"]
    assert await _audit("secrets.execute_halted", "deny")

    monkeypatch.setattr(tasks, "authorize_item", AUTHORIZE)
    if how == "revoke":
        await _restore(w, "op", "secrets_operator")
        await w.call("op", "POST", f"/plans/{p['id']}/retry", {}, expect=202)
        await run_jobs()
        done = await w.get(p["id"])
        assert done["status"] == "succeeded" and "halted" not in done["summary"]
        assert len(w.sm.secrets[ms[0]["aws_secret_name"]]["versions"]) == 1  # the completed item was not written again
    else:  # an expired approval or a changed connector needs a fresh comparison and review
        r = await w.call("op", "POST", f"/plans/{p['id']}/retry", {})
        assert r.status_code == 409


async def test_a_retry_after_revocation_reconciles_the_ambiguous_write_without_a_second_version(world, monkeypatch):
    w = world
    ms, p = await _three(w)
    orig_create, calls = w.sm.create_secret, {"n": 0}

    def create(**kw):  # b's create lands and its answer is lost; the others answer
        calls["n"] += 1
        if calls["n"] != 2:
            w.sm.timeout_after.discard("create_secret")
        else:
            w.sm.timeout_after.add("create_secret")
        return orig_create(**kw)

    w.sm.create_secret = create
    _pause_after(monkeypatch, 4, lambda: _change(w, "revoke", p["id"]))  # after b's pre-write check: b writes, then c halts
    await w.approve_and_run(p, run=False)
    await run_jobs()
    done = await w.get(p["id"], "ap")
    by_name = {i["mapping"]["secret_name"]: i for i in done["items"]}
    assert [by_name[n]["status"] for n in "abc"] == ["succeeded", "failed", "failed"]
    assert by_name["b"]["write_state"]["aws"] == "intent" and "not known" in by_name["b"]["error"]
    assert done["summary"]["halted"]["ambiguous"] == [by_name["b"]["id"]] and done["summary"]["halted"]["not_run"] == [by_name["c"]["id"]]
    assert ms[2]["aws_secret_name"] not in w.sm.secrets
    w.sm.create_secret = orig_create
    w.sm.timeout_after.discard("create_secret")
    monkeypatch.setattr(tasks, "authorize_item", AUTHORIZE)
    await _restore(w, "op", "secrets_operator")
    await w.call("op", "POST", f"/plans/{p['id']}/retry", {}, expect=202)
    await run_jobs()
    done = await w.get(p["id"])
    assert done["status"] == "succeeded", done
    assert [len(w.sm.secrets[m["aws_secret_name"]]["versions"]) for m in ms] == [1, 1, 1]  # b was found, not written twice


async def test_a_plan_compared_by_the_old_version_is_refused(world):
    w = world
    w.east.put("shop", "db", {"p": b"one"})
    await w.discover()
    m = await w.mapping("db", direction="export")
    p = await w.plan([m["id"]])
    async with session_scope() as db:
        await db.execute(text("UPDATE secret_plans SET hash_version = 1 WHERE id = :p"), {"p": uuid.UUID(p["id"])})
    r = await w.call("op", "POST", f"/plans/{p['id']}/submit", {"version": p["version"]})
    assert r.status_code == 409 and "older MOKS version" in r.text
    assert (await w.call("op", "POST", f"/plans/{p['id']}/refresh", expect=201)).json()["status"] == "comparing"


# ---- manual-only policy ---------------------------------------------------------------------------------------


async def test_manual_policy_holds_schedules_visibly_and_needs_a_fresh_approval(world, monkeypatch):
    w = world
    s_ = get_settings()
    assert s_.secrets_sync_policy == "manual"
    m, _ = await _export_one(w, {"p": b"one"}, direction="export")
    r = await w.call("op", "POST", "/schedules", {"name": "nope", "mapping_ids": [m["id"]], "interval_minutes": 30})
    assert r.status_code == 409 and "manual-only" in r.text
    scope = (await w.call("op", "GET", "/scope", expect=200)).json()
    assert scope["policy"]["mode"] == "manual" and not scope["policy"]["schedules_run"]

    monkeypatch.setattr(s_, "secrets_sync_policy", "scheduled")  # a schedule approved while scheduling was allowed
    s = (await w.call("op", "POST", "/schedules", {"name": "old", "mapping_ids": [m["id"]], "interval_minutes": 30}, expect=201)).json()
    s = (await w.call("ap", "POST", f"/schedules/{s['id']}/decision", {"approve": True, "schedule_hash": s["schedule_hash"]}, expect=200)).json()
    assert s["status"] == "active"

    monkeypatch.setattr(s_, "secrets_sync_policy", "manual")
    listed = (await w.call("op", "GET", "/schedules", expect=200)).json()["items"][0]
    assert listed["status"] == "held" and listed["held"] and "manual-only" in listed["paused_reason"] and not listed["allowed"]["run"]
    w.east.put("shop", "db", {"p": b"two"})
    old_role, s_.process_role = s_.process_role, "executor"
    try:
        assert await tasks.hold_schedules() == 1
    finally:
        s_.process_role = old_role
    assert await run_schedules() == []
    assert w.aws_content(m["aws_secret_name"]).data == {"p": b"one"}  # nothing written
    for action in ("run", "resume"):
        assert (await w.call("op", "POST", f"/schedules/{s['id']}/{action}")).status_code == 409
    assert (await w.call("ap", "POST", f"/schedules/{s['id']}/decision", {"approve": True, "schedule_hash": s["schedule_hash"]})).status_code == 409
    async with session_scope() as db:  # history stays
        n = (await db.execute(text("SELECT count(*) FROM secret_schedule_approvals WHERE schedule_id = :s"), {"s": uuid.UUID(s["id"])})).scalar_one()
        held = (await db.execute(text("SELECT status FROM secret_sync_schedules WHERE id = :s"), {"s": uuid.UUID(s["id"])})).scalar_one()
    assert n == 1 and held == "held"
    assert await _audit("secrets.schedule_hold")
    # a manual sync plan still works
    p = await w.plan([m["id"]])
    assert (await w.approve_and_run(p))["status"] == "succeeded"

    # back to scheduled: the held schedule does not resume on its old approval
    monkeypatch.setattr(s_, "secrets_sync_policy", "scheduled")
    w.east.put("shop", "db", {"p": b"three"})
    assert await run_schedules() == []
    listed = (await w.call("ap", "GET", "/schedules", expect=200)).json()["items"][0]
    assert listed["needs_fresh_approval"] and listed["allowed"]["approve"]
    await w.call("ap", "POST", f"/schedules/{s['id']}/decision", {"approve": True, "schedule_hash": s["schedule_hash"]}, expect=200)
    (run,) = await run_schedules()
    assert run["status"] == "succeeded" and w.aws_content(m["aws_secret_name"]).data == {"p": b"three"}
    await w.call("op", "DELETE", f"/schedules/{s['id']}", expect=200)  # schedules are due across environments; leave none behind


async def test_a_queued_scheduled_run_is_refused_by_the_executor_under_the_manual_policy(world, monkeypatch):
    from moks.secrets import service

    w = world
    s_ = get_settings()
    monkeypatch.setattr(s_, "secrets_sync_policy", "scheduled")
    m, _ = await _export_one(w, {"p": b"one"}, direction="export")
    s = (await w.call("op", "POST", "/schedules", {"name": "q", "mapping_ids": [m["id"]], "interval_minutes": 30}, expect=201)).json()
    await w.call("ap", "POST", f"/schedules/{s['id']}/decision", {"approve": True, "schedule_hash": s["schedule_hash"]}, expect=200)
    monkeypatch.setattr(s_, "secrets_sync_policy", "manual")
    async with session_scope() as db:
        from moks.models import SecretPlan, SecretSchedule

        sched = await db.get(SecretSchedule, uuid.UUID(s["id"]))
        plan = SecretPlan(environment_id=sched.environment_id, kind="sync", status="queued", requested_by=sched.requested_by, executed_by=sched.requested_by,
                          request={"mapping_ids": [m["id"]], "schedule_id": s["id"], "schedule_hash": sched.schedule_hash}, hash_version=service.HASH_VERSION)
        db.add(plan)
        await db.flush()
        plan.plan_hash = service.compute_hash(plan, w.env, [])
        pid = str(plan.id)
    old_role, s_.process_role = s_.process_role, "executor"
    try:
        out = await tasks.execute_plan({"plan_id": pid, "_job_id": str(uuid.uuid4())})
    finally:
        s_.process_role = old_role
    assert "manual-only" in out["error"]
    async with session_scope() as db:
        await db.execute(text("UPDATE secret_sync_schedules SET status = 'retired' WHERE id = :s"), {"s": uuid.UUID(s["id"])})
