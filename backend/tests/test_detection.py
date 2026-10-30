"""Detection and automatic incidents: dedup, sustain, suppression, flapping, missing telemetry."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from moks import detection, skills
from moks.api.environments import sync_automation_binding
from moks.connectors import registry
from moks.db import session_scope
from moks.detection import Check, Rule, Signal
from moks.models import DetectionRun, DetectionState, Environment, Incident, IncidentEvent, Investigation, MaintenanceWindow, User
from tests.conftest import login, make_env, make_user
from tests.fakes import FakeKubernetes


def rule(**kw):
    meta = skills.SkillMeta(name="test-skill", description="a rule used in these tests", version="3", detection=[], runbooks=["docs/x.md"], owner="payments")
    spec = skills.DetectionSpec(id=f"r-{uuid.uuid4().hex[:6]}", kind="finding", finding="crashloop", sustained_checks=2, recovery_checks=2, cooldown_minutes=30, **kw)
    return Rule(spec, skills.Skill(meta=meta, body="", path="x", source="builtin"))


@pytest.fixture(autouse=True)
def fake_k8s(monkeypatch):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "kubernetes", FakeKubernetes)
    registry._cache.clear()
    yield
    registry._cache.clear()


async def _env(detect=True):
    name = f"det-{uuid.uuid4().hex[:6]}"
    env_id = await make_env(name, connectors=[{"type": "kubernetes", "name": "k8s", "config": {}}])
    async with session_scope() as db:
        env = await db.get(Environment, env_id)
        env.detection_enabled = detect
        await sync_automation_binding(db, name, True)
    return env_id


FIRING = Check("firing", [Signal("pod/payments-1", "shop", "payments is crashing", evidence=[{"tool": "k8s_triage"}])])
CLEAR = Check("ok")
UNKNOWN = Check("unknown", reason="prometheus is down")


async def _apply(env_id, r, check, at=None):
    async with session_scope() as db:
        env = await db.get(Environment, env_id)
        return await detection.apply(db, env, r, r.spec, check, at or datetime.now(timezone.utc))


async def _incidents(env_id):
    async with session_scope() as db:
        return (await db.execute(select(Incident).where(Incident.environment_id == env_id))).scalars().all()


async def test_one_bad_check_opens_nothing_and_a_sustained_one_opens_one():
    env, r = await _env(), rule()
    assert (await _apply(env, r, FIRING))["opened"] == 0
    assert not await _incidents(env)
    assert (await _apply(env, r, FIRING))["opened"] == 1
    inc = (await _incidents(env))[0]
    assert inc.source == "detection" and inc.rule_id == r.id and inc.rule_version == "3" and inc.skill == "test-skill"
    assert inc.resource == "pod/payments-1" and inc.owner_team == "payments" and inc.first_observed_at and "2 consecutive checks" in inc.severity_reason
    assert inc.runbook == "docs/x.md"


async def test_repeated_detections_update_one_incident():
    env, r = await _env(), rule()
    for _ in range(6):
        await _apply(env, r, FIRING)
    rows = await _incidents(env)
    assert len(rows) == 1 and rows[0].detection_count == 6


async def test_different_resources_get_different_incidents():
    env, r = await _env(), rule()
    two = Check("firing", [Signal("pod/a", "shop", "x", evidence=[{}]), Signal("pod/b", "shop", "y", evidence=[{}])])
    await _apply(env, r, two)
    await _apply(env, r, two)
    assert len(await _incidents(env)) == 2


async def test_duplicate_workers_cannot_open_two_incidents():
    env, r = await _env(), rule()
    await _apply(env, r, FIRING)
    await _apply(env, r, FIRING)
    fp = detection.fingerprint(r.id, env, "pod/payments-1")
    async with session_scope() as db:
        e = await db.get(Environment, env)
        st = await db.get(DetectionState, fp)
        sig = FIRING.signals[0]
        again = await detection._open_incident(db, e, r, r.spec, sig, st, datetime.now(timezone.utc))
    assert len(await _incidents(env)) == 1 and again.fingerprint == fp


async def test_missing_telemetry_is_not_health_and_not_failure():
    env, r = await _env(), rule(auto_resolve=True)
    await _apply(env, r, FIRING)
    await _apply(env, r, FIRING)
    for _ in range(10):
        await _apply(env, r, UNKNOWN)
    inc = (await _incidents(env))[0]
    assert inc.status == "open"  # a Prometheus outage never reports recovery
    fresh = await _env()
    for _ in range(10):
        await _apply(fresh, r, UNKNOWN)
    assert not await _incidents(fresh)  # and never opens an incident


async def test_partial_read_cannot_clear_what_it_did_not_see():
    env, r = await _env(), rule(auto_resolve=True)
    await _apply(env, r, FIRING)
    await _apply(env, r, FIRING)
    for _ in range(5):
        await _apply(env, r, Check("ok", partial=True))
    assert (await _incidents(env))[0].status == "open"


async def test_recovery_needs_consecutive_clear_checks():
    env, r = await _env(), rule(auto_resolve=True)
    await _apply(env, r, FIRING)
    await _apply(env, r, FIRING)
    await _apply(env, r, CLEAR)
    assert (await _incidents(env))[0].status == "open"
    await _apply(env, r, FIRING)  # flap resets the count
    await _apply(env, r, CLEAR)
    assert (await _incidents(env))[0].status == "open"
    await _apply(env, r, CLEAR)
    inc = (await _incidents(env))[0]
    assert inc.status == "resolved" and inc.resolved_at


async def test_without_auto_resolve_a_person_closes_the_incident():
    env, r = await _env(), rule(auto_resolve=False)
    for c in (FIRING, FIRING, CLEAR, CLEAR, CLEAR):
        await _apply(env, r, c)
    inc = (await _incidents(env))[0]
    assert inc.status == "open"
    async with session_scope() as db:
        events = (await db.execute(select(IncidentEvent).where(IncidentEvent.incident_id == inc.id))).scalars().all()
    assert any("Condition clear" in e.payload.get("text", "") for e in events)


async def test_flapping_inside_the_cooldown_reopens_instead_of_creating_a_storm():
    env, r = await _env(), rule(auto_resolve=True)
    for c in (FIRING, FIRING, CLEAR, CLEAR, FIRING, FIRING, CLEAR, CLEAR, FIRING, FIRING):
        await _apply(env, r, c)
    rows = await _incidents(env)
    assert len(rows) == 1 and rows[0].status == "open"


async def test_maintenance_window_suppresses_then_detection_resumes():
    env, r = await _env(), rule()
    now = datetime.now(timezone.utc)
    async with session_scope() as db:
        u = (await db.execute(select(User).limit(1))).scalar_one_or_none()
        if u is None:
            u = User(email=f"m-{uuid.uuid4().hex[:6]}@example.com", display_name="m")
            db.add(u)
            await db.flush()
        w = MaintenanceWindow(environment_id=env, namespace="shop", starts_at=now - timedelta(minutes=1), ends_at=now + timedelta(minutes=30), reason="upgrade", created_by=u.id)
        db.add(w)
    assert (await _apply(env, r, FIRING))["suppressed"] == 1
    await _apply(env, r, FIRING)
    assert not await _incidents(env)
    await _apply(env, r, FIRING, now + timedelta(hours=1))
    assert len(await _incidents(env)) == 1


async def test_auto_investigation_is_capped_and_never_acts():
    env, r = await _env(), rule(auto_investigate=True)
    many = Check("firing", [Signal(f"pod/{i}", "shop", "x", evidence=[{}]) for i in range(10)])
    await _apply(env, r, many)
    await _apply(env, r, many)
    async with session_scope() as db:
        runs = (await db.execute(select(Investigation).where(Investigation.environment_id == env, Investigation.trigger == "detection"))).scalars().all()
        from moks.models import ActionPlan

        plans = (await db.execute(select(ActionPlan).where(ActionPlan.environment_id == env))).scalars().all()
    assert len(runs) == detection.MAX_AUTO_PER_HOUR and not plans
    assert len(await _incidents(env)) == 10


async def test_rule_schema_cannot_carry_an_action():
    with pytest.raises(ValueError):
        skills.DetectionSpec(id="bad-rule", kind="finding", finding="crashloop", action="restart_deployment")
    with pytest.raises(ValueError):
        skills.DetectionSpec(id="bad-rule", kind="prometheus")


async def test_live_finding_rule_evaluates_through_the_executor():
    env = await _env()
    r = rule()
    async with session_scope() as db:
        e = await db.get(Environment, env)
        check = await detection.evaluate(db, e, r.spec)
    assert check.status == "firing" and any("payments" in s.resource for s in check.signals)
    assert check.partial  # the fake lists kube-system but cannot read it


async def test_evaluate_without_connector_is_unknown_not_healthy():
    r = rule()
    name = f"det-{uuid.uuid4().hex[:6]}"
    env_id = await make_env(name)
    async with session_scope() as db:
        await sync_automation_binding(db, name, True)
    async with session_scope() as db:
        e = await db.get(Environment, env_id)
        assert (await detection.evaluate(db, e, r.spec)).status == "unknown"


async def test_tick_respects_the_interval_across_restarts(monkeypatch):
    env, r = await _env(), rule()
    calls = []

    async def fake_eval(db, e, spec):
        calls.append(spec.id)
        return CLEAR
    monkeypatch.setattr(detection, "all_rules", lambda: [r])
    monkeypatch.setattr(detection, "evaluate", fake_eval)
    await detection.tick(env)
    await detection.tick(env)  # a second scheduler run, or a restarted one
    assert len(calls) == 1
    async with session_scope() as db:
        assert (await db.get(DetectionRun, (r.id, env))).last_status == "ok"


async def test_detection_off_means_no_checks(monkeypatch):
    env = await _env(detect=False)
    assert "skipped" in await detection.tick(env)


async def test_dry_run_writes_nothing_and_previews(client):
    env = await _env()
    tag = uuid.uuid4().hex[:6]
    async with session_scope() as db:
        name = (await db.get(Environment, env)).name
    await make_user(f"adm-{tag}@example.com", ["admin"])
    h = await login(client, f"adm-{tag}@example.com")
    rules = (await client.get("/moks/api/detections/rules")).json()["rules"]
    rid = next(x["id"] for x in rules if x["kind"] == "finding" and x["finding"] == "crashloop")
    r = (await client.post(f"/moks/api/detections/rules/{rid}/test", json={"environment": name}, headers=h)).json()
    assert r["status"] == "firing" and r["preview"][0]["would"].startswith("wait") and "never authorizes" in r["note"]
    assert not await _incidents(env)
    async with session_scope() as db:
        assert not (await db.execute(select(DetectionState).where(DetectionState.environment_id == env))).scalars().all()
    bad = await client.put(f"/moks/api/detections/rules/{rid}", json={"overrides": {"severity": "sev9"}}, headers=h)
    assert bad.status_code == 422
    ok = await client.put(f"/moks/api/detections/rules/{rid}", json={"enabled": False, "overrides": {"interval_minutes": 10}}, headers=h)
    assert ok.status_code == 200
    await client.put(f"/moks/api/detections/rules/{rid}", json={"enabled": True}, headers=h)


async def test_only_admins_change_policy_or_dry_run(client):
    env = await _env()
    tag = uuid.uuid4().hex[:6]
    await make_user(f"v-{tag}@example.com", ["investigator:*:*"])
    h = await login(client, f"v-{tag}@example.com")
    rules = (await client.get("/moks/api/detections/rules")).json()["rules"]
    rid = rules[0]["id"]
    assert (await client.put(f"/moks/api/detections/rules/{rid}", json={"enabled": False}, headers=h)).status_code == 403
    assert (await client.post(f"/moks/api/detections/rules/{rid}/test", json={"environment": "x"}, headers=h)).status_code == 403
