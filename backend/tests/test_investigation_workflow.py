"""Investigation workflow: honest labels, stages, follow-ups, grouped runs, coverage, handover,
skill availability, and end-to-end scenarios from fixtures."""
import uuid

import pytest
from sqlalchemy import select

from moks.connectors import registry
from moks.db import session_scope
from moks.models import Environment, Incident, Investigation, SkillPolicy
from moks.orchestrator.engine import Investigator
from moks.orchestrator.report import Cause, Report, verify_causes
from tests.conftest import login, make_env, make_user
from tests.fakes import SCENARIOS, FakeKubernetes, pod


@pytest.fixture(autouse=True)
def fake_k8s(monkeypatch):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "kubernetes", FakeKubernetes)
    registry._cache.clear()
    yield
    registry._cache.clear()


def test_a_confident_model_cannot_verify_a_cause():
    report = Report(suspected_causes=[Cause(cause="the database is down", confidence="high", evidence=["E1"], verified=True)])
    verify_causes(report, [{"kind": "crashloop", "title": "x is crashing", "evidence": ["E1"]}])
    assert report.suspected_causes[0].verified is False and report.verified_causes == []


def test_a_direct_detector_observation_is_verified_with_its_check_named():
    report = Report()
    verify_causes(report, [{"kind": "service_selector_mismatch", "title": "Service checkout selector matches no pods", "evidence": ["E2"], "resources": ["service/checkout"]}])
    assert report.verified_causes[0]["check"] == "deterministic detector: service_selector_mismatch"


def test_a_finding_without_evidence_is_not_verified():
    report = Report()
    verify_causes(report, [{"kind": "oomkilled", "title": "x", "evidence": []}])
    assert report.verified_causes == []


async def _env(user_tag=None, **conn):
    name = f"wf-{uuid.uuid4().hex[:6]}"
    await make_env(name, connectors=[{"type": "kubernetes", "name": "k8s", "config": {}}])
    return name


async def _run(inv_id):
    await Investigator(uuid.UUID(inv_id), "t").run()
    async with session_scope() as db:
        return await db.get(Investigation, uuid.UUID(inv_id))


async def test_stages_coverage_relationships_and_verified_cause_on_a_real_run(client):
    env = await _env()
    tag = uuid.uuid4().hex[:6]
    await make_user(f"a-{tag}@example.com", [f"investigator:{env}:*"])
    h = await login(client, f"a-{tag}@example.com")
    r = (await client.post("/moks/api/investigations", json={"question": "why is checkout returning 502", "environment": env, "namespace": "shop"}, headers=h)).json()
    inv = await _run(r["id"])
    assert inv.status == "completed" and inv.stage == "report"
    rep = inv.report
    assert any("selector matches no pods" in c["cause"] for c in rep["verified_causes"])
    assert all(c["verified"] is False for c in rep["suspected_causes"])
    assert rep["coverage"]["sources"]["k8s"]["ok"] > 0 and rep["coverage"]["tool_calls"] > 0
    assert rep["relationships"]["nodes"] and rep["primary_resource"]
    detail = (await client.get(f"/moks/api/investigations/{r['id']}")).json()
    assert detail["stage"] == "report" and detail["actions"] == [] and detail["follow_ups"] == []


async def test_follow_up_keeps_scope_and_links_to_its_parent(client):
    env = await _env()
    tag = uuid.uuid4().hex[:6]
    await make_user(f"b-{tag}@example.com", [f"investigator:{env}:shop"])
    h = await login(client, f"b-{tag}@example.com")
    r = (await client.post("/moks/api/investigations", json={"question": "why is payments crashing", "environment": env, "namespace": "shop"}, headers=h)).json()
    await _run(r["id"])
    f = (await client.post(f"/moks/api/investigations/{r['id']}/follow-up", json={"question": "and since when?"}, headers=h)).json()
    assert f["environment"] == env and f["namespace"] == "shop"
    assert (await _run(f["id"])).status == "completed"
    parent = (await client.get(f"/moks/api/investigations/{r['id']}")).json()
    assert parent["follow_ups"][0]["id"] == f["id"]
    assert (await client.get(f"/moks/api/investigations/{f['id']}")).json()["parent_id"] == r["id"]


async def test_group_runs_only_where_authorized(client):
    a, b, c = await _env(), await _env(), await _env()
    tag = uuid.uuid4().hex[:6]
    await make_user(f"c-{tag}@example.com", [f"investigator:{a}:*", f"investigator:{b}:*"])
    h = await login(client, f"c-{tag}@example.com")
    r = (await client.post("/moks/api/investigations/group", json={"question": "is anything crashing"}, headers=h)).json()
    assert {i["environment"] for i in r["investigations"]} == {a, b}
    assert (await client.post("/moks/api/investigations/group", json={"question": "is anything crashing", "environments": [a, c]}, headers=h)).status_code == 403


async def test_follow_up_is_not_available_to_someone_who_cannot_see_the_parent(client):
    env = await _env()
    tag = uuid.uuid4().hex[:6]
    await make_user(f"d-{tag}@example.com", [f"investigator:{env}:shop"])
    await make_user(f"e-{tag}@example.com", [f"investigator:{env}:secure"])
    h = await login(client, f"d-{tag}@example.com")
    r = (await client.post("/moks/api/investigations", json={"question": "why crashing", "environment": env, "namespace": "shop"}, headers=h)).json()
    h = await login(client, f"e-{tag}@example.com")
    assert (await client.post(f"/moks/api/investigations/{r['id']}/follow-up", json={"question": "tell me"}, headers=h)).status_code == 404


async def test_handover_keeps_facts_and_suspicions_apart_and_assignment_is_audited(client):
    env = await _env()
    tag = uuid.uuid4().hex[:6]
    await make_user(f"f-{tag}@example.com", [f"operator:{env}:shop"])
    h = await login(client, f"f-{tag}@example.com")
    inc = (await client.post("/moks/api/incidents", json={"title": "checkout 502", "environment": env, "namespace": "shop", "severity": "sev2"}, headers=h)).json()
    r = (await client.post("/moks/api/investigations", json={"question": "why is checkout returning 502", "environment": env, "namespace": "shop", "incident_id": inc["id"]}, headers=h)).json()
    await _run(r["id"])
    assert (await client.patch(f"/moks/api/incidents/{inc['id']}", json={"commander": "nobody@nowhere.example"}, headers=h)).status_code == 422
    d = (await client.patch(f"/moks/api/incidents/{inc['id']}", json={"commander": f"f-{tag}@example.com"}, headers=h)).json()
    assert d["commander"] == f"f-{tag}@example.com"
    text = (await client.get(f"/moks/api/incidents/{inc['id']}/handover.md")).text
    assert "## Verified causes" in text and "## Suspected causes (not verified)" in text and "## Validated recovery\nNot validated." in text


async def test_skills_show_why_they_are_unavailable_and_admins_can_turn_them_off(client):
    env = await _env()
    tag = uuid.uuid4().hex[:6]
    await make_user(f"g-{tag}@example.com", ["admin"])
    h = await login(client, f"g-{tag}@example.com")
    d = (await client.get("/moks/api/skills/rds-pressure")).json()
    assert d["availability"][env]["available"] is False and "needs the cloudwatch connector" in d["availability"][env]["reasons"]
    assert (await client.put("/moks/api/skills/crashloop-triage/policy", json={"enabled": False}, headers=h)).status_code == 200
    r = (await client.post("/moks/api/investigations", json={"question": "why is payments crashing", "environment": env, "namespace": "shop"}, headers=h)).json()
    inv = await _run(r["id"])
    assert "crashloop-triage" not in [s["name"] for s in inv.report["skills"]]
    await client.put("/moks/api/skills/crashloop-triage/policy", json={"enabled": True}, headers=h)
    async with session_scope() as db:
        assert (await db.get(SkillPolicy, "crashloop-triage")).enabled


async def test_failed_rollout_scenario(client, monkeypatch):
    env = await _env()
    tag = uuid.uuid4().hex[:6]
    scenario = {"pods": [pod("orders-new-abc", "rollout", {"app": "orders"}, waiting="ImagePullBackOff", ready=False)], "services": [],
                "deployments": [{"name": "orders", "replicas": 2, "ready": 1}], "logs": "ok\n"}
    monkeypatch.setitem(SCENARIOS, "rollout", scenario)
    await make_user(f"h-{tag}@example.com", [f"investigator:{env}:*"])
    h = await login(client, f"h-{tag}@example.com")
    r = (await client.post("/moks/api/investigations", json={"question": "the orders rollout is stuck", "environment": env, "namespace": "rollout"}, headers=h)).json()
    inv = await _run(r["id"])
    kinds = {f["kind"] for f in inv.report["findings"]}
    assert "image_pull" in kinds and "rollout-failures" in [s["name"] for s in inv.report["skills"]] or "image-pull-failures" in [s["name"] for s in inv.report["skills"]]
    assert any("cannot pull" in c["cause"] for c in inv.report["verified_causes"])


async def test_repeated_detection_of_a_real_fixture_produces_one_incident(client):
    from moks import detection
    from moks.api.environments import sync_automation_binding

    env_name = await _env()
    async with session_scope() as db:
        env = (await db.execute(select(Environment).where(Environment.name == env_name))).scalar_one()
        env.detection_enabled = True
        await sync_automation_binding(db, env_name, True)
        env_id = env.id
    rule = next(r for r in detection.all_rules() if r.id == "pod-crashloop")
    for _ in range(5):
        async with session_scope() as db:
            e = await db.get(Environment, env_id)
            check = await detection.evaluate(db, e, rule.spec)
            await detection.apply(db, e, rule, rule.spec, check, __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    async with session_scope() as db:
        rows = (await db.execute(select(Incident).where(Incident.environment_id == env_id))).scalars().all()
    assert len(rows) == 1 and rows[0].rule_id == "pod-crashloop" and rows[0].detection_count == 5
