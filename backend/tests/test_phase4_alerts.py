"""Phase 4: alert intake, deduplication, cooldown, rate cap, automation scope, incident reports."""
import uuid

import pytest
from sqlalchemy import select

from moks.config import get_settings
from moks.db import session_scope
from moks.models import AlertEvent, Environment, Incident, Investigation
from tests.conftest import login, make_env, make_user


def payload(name="HighErrorRate", status="firing", ns="shop", **labels):
    return {"alerts": [{"status": status, "labels": {"alertname": name, "severity": "critical", "namespace": ns, "pod": f"p-{uuid.uuid4().hex[:4]}", **labels},
                        "annotations": {"summary": "5xx above 10%"}, "startsAt": "2026-10-30T10:00:00Z"}]}


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(get_settings(), "alert_webhook_token", "hook-secret-123")
    return {"Authorization": "Bearer hook-secret-123"}


async def _env(auto=True):
    name = f"al-{uuid.uuid4().hex[:6]}"
    env_id = await make_env(name)
    async with session_scope() as db:
        e = await db.get(Environment, env_id)
        e.auto_investigate, e.alert_cooldown_minutes = auto, 30
        from moks.api.environments import sync_automation_binding

        await sync_automation_binding(db, name, auto)
    return name, env_id


async def test_webhook_requires_token(client, token):
    name, _ = await _env()
    assert (await client.post(f"/moks/api/alerts/webhook/{name}", json=payload())).status_code == 401
    assert (await client.post(f"/moks/api/alerts/webhook/{name}", json=payload(), headers={"Authorization": "Bearer wrong"})).status_code == 401


async def test_alert_opens_incident_investigates_then_deduplicates(client, token):
    name, env_id = await _env()
    r1 = (await client.post(f"/moks/api/alerts/webhook/{name}", json=payload(), headers=token)).json()
    d1 = r1["decisions"][0]
    assert d1["decision"] == "investigated" and d1["incident_id"] and d1["investigation_id"]
    async with session_scope() as db:
        inv = await db.get(Investigation, uuid.UUID(d1["investigation_id"]))
        assert inv.trigger == "alert" and inv.namespace == "shop" and inv.status == "queued" and str(inv.incident_id) == d1["incident_id"]
    # same alert again (different pod label = same fingerprint): absorbed by the open incident
    d2 = (await client.post(f"/moks/api/alerts/webhook/{name}", json=payload(), headers=token)).json()["decisions"][0]
    assert d2["decision"] == "deduplicated" and d2["incident_id"] == d1["incident_id"]
    d3 = (await client.post(f"/moks/api/alerts/webhook/{name}", json=payload(status="resolved"), headers=token)).json()["decisions"][0]
    assert d3["decision"] == "resolved"
    # incident closed, alert fires again within the cooldown: no new investigation
    async with session_scope() as db:
        (await db.get(Incident, uuid.UUID(d1["incident_id"]))).status = "closed"
    d4 = (await client.post(f"/moks/api/alerts/webhook/{name}", json=payload(), headers=token)).json()["decisions"][0]
    assert d4["decision"] == "cooldown"
    async with session_scope() as db:
        assert (await db.execute(select(Investigation).where(Investigation.environment_id == env_id))).scalars().all().__len__() == 1


async def test_rate_cap_and_no_auto_investigation_without_opt_in(client, token):
    from moks import alerts

    name, env_id = await _env()
    decisions = []
    for i in range(alerts.MAX_AUTO_PER_HOUR + 2):
        r = (await client.post(f"/moks/api/alerts/webhook/{name}", json=payload(name=f"Alert{i}"), headers=token)).json()
        decisions.append(r["decisions"][0]["decision"])
    assert decisions.count("investigated") == alerts.MAX_AUTO_PER_HOUR and "rate_limited" in decisions
    quiet, _ = await _env(auto=False)
    d = (await client.post(f"/moks/api/alerts/webhook/{quiet}", json=payload(), headers=token)).json()["decisions"][0]
    assert d["decision"] == "incident" and "investigation_id" not in d


async def test_automation_scope_follows_environment_setting(client, token):
    name, env_id = await _env(auto=True)
    async with session_scope() as db:
        e = await db.get(Environment, env_id)
        e.denied_namespaces = ["secure"]
    d = (await client.post(f"/moks/api/alerts/webhook/{name}", json=payload(ns="secure"), headers=token)).json()["decisions"][0]
    assert d["decision"] == "not_permitted"


async def test_incident_workspace_report_and_postmortem_feed_knowledge(client, token):
    name, _ = await _env()
    tag = uuid.uuid4().hex[:6]
    await make_user(f"ic-{tag}@example.com", [f"investigator:{name}:*", f"approver:{name}:*"])
    h = await login(client, f"ic-{tag}@example.com")
    inc = (await client.post("/moks/api/incidents", json={"title": f"Checkout 502s {tag}", "environment": name, "namespace": "shop", "severity": "sev2"},
                             headers=h)).json()
    await client.post(f"/moks/api/incidents/{inc['id']}/events", json={"text": "Selector typo found in service/checkout", "kind": "evidence"}, headers=h)
    await client.patch(f"/moks/api/incidents/{inc['id']}", json={"hypotheses": [{"text": "selector mismatch", "confidence": "high", "status": "supported"}]}, headers=h)
    pm = {"impact": "Checkout unavailable 14 min", "root_cause": f"Service selector typo marmot{tag}", "root_cause_verified": True,
          "action_items": ["Add selector check to CI"]}
    assert (await client.put(f"/moks/api/incidents/{inc['id']}/postmortem", json=pm, headers=h)).status_code == 200
    assert (await client.post(f"/moks/api/incidents/{inc['id']}/postmortem/approve", headers=h)).status_code == 409  # still open
    await client.patch(f"/moks/api/incidents/{inc['id']}", json={"status": "resolved"}, headers=h)
    approved = await client.post(f"/moks/api/incidents/{inc['id']}/postmortem/approve", headers=h)
    assert approved.json()["postmortem_status"] == "approved"
    md = (await client.get(f"/moks/api/incidents/{inc['id']}/report.md")).text
    assert "## Root cause" in md and "## Timeline" in md and "Selector typo found" in md and "- [ ] Add selector check to CI" in md
    hits = (await client.get("/moks/api/knowledge/search", params={"q": f"marmot{tag}"})).json()["results"]
    assert hits and hits[0]["citation"]["source_type"] == "incident" and hits[0]["scope"]["namespace"] == "shop"


async def test_poller_only_resolves_incidents_it_opened(client, token, monkeypatch):
    from moks import alerts
    from moks.connectors import registry

    name, env_id = await _env(auto=False)
    await client.post(f"/moks/api/alerts/webhook/{name}", json=payload(name="FromWebhook"), headers=token)

    class Prom:
        firing = [{"labels": {"alertname": "FromPoll", "namespace": "shop"}, "annotations": {}, "state": "firing", "activeAt": None}]

        async def active_alerts(self):
            return Prom.firing

    async def fake_for_env(db, eid):
        return {"prometheus": (None, Prom())}, {}

    monkeypatch.setattr(registry, "for_environment", fake_for_env)
    r1 = await alerts.poll_environment(env_id)
    assert r1["decisions"] == ["incident"] and r1["resolved"] == 0
    Prom.firing = []  # nothing firing in Prometheus any more
    r2 = await alerts.poll_environment(env_id)
    assert r2["resolved"] == 1  # only the poll-opened incident
    r3 = await alerts.poll_environment(env_id)
    assert r3["resolved"] == 0  # resolved once, not on every poll
    async with session_scope() as db:
        incs = {i.title.split(":")[0]: i.source for i in (await db.execute(select(Incident).where(Incident.environment_id == env_id))).scalars()}
        resolved = (await db.execute(select(AlertEvent.alertname).where(AlertEvent.environment_id == env_id, AlertEvent.decision == "resolved"))).scalars().all()
    assert incs == {"FromWebhook": "alert-webhook", "FromPoll": "alert-poll"} and resolved == ["FromPoll"]
