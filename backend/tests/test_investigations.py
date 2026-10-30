"""End-to-end investigation flow against the fake Kubernetes connector: scoping, evidence,
deterministic diagnosis, SSE streaming, cancellation, time limits and prompt injection."""
import asyncio
import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from moks.connectors import registry
from moks.db import session_scope
from moks.models import AuditEvent, Investigation, LLMConfig
from moks.orchestrator.engine import Investigator
from tests.conftest import login, make_env, make_user
from tests.fakes import FakeKubernetes


@pytest.fixture(autouse=True)
def fake_k8s(monkeypatch):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "kubernetes", FakeKubernetes)
    registry._cache.clear()
    FakeKubernetes.delay = 0.0
    FakeKubernetes.calls = []
    yield
    registry._cache.clear()


async def _env(prefix: str, **kw) -> str:
    name = f"{prefix}-{uuid.uuid4().hex[:6]}"
    await make_env(name, connectors=[{"type": "kubernetes", "name": "k8s", "config": {"mode": "in_cluster"}}], **kw)
    return name


async def _run(inv_id: str) -> Investigation:
    await Investigator(uuid.UUID(inv_id), "test-worker").run()
    async with session_scope() as db:
        return await db.get(Investigation, uuid.UUID(inv_id))


async def test_namespace_scoped_user_cannot_investigate_other_namespaces(client):
    env = await _env("scoped")
    await make_user("ns-user@example.com", [f"investigator:{env}:shop"])
    h = await login(client, "ns-user@example.com")
    ok = await client.post("/moks/api/investigations", json={"question": "why 502?", "environment": env, "namespace": "shop"}, headers=h)
    assert ok.status_code == 201
    for ns in ("secure", "kube-system", "*"):
        r = await client.post("/moks/api/investigations", json={"question": "what broke?", "environment": env, "namespace": ns}, headers=h)
        assert r.status_code == 403, ns


async def test_environment_denylist_overrides_rbac(client):
    env = await _env("deny", denied=("secure",))
    await make_user("wide@example.com", [f"investigator:{env}:*"])
    h = await login(client, "wide@example.com")
    r = await client.post("/moks/api/investigations", json={"question": "what broke?", "environment": env, "namespace": "secure"}, headers=h)
    assert r.status_code == 403


async def test_investigations_are_invisible_outside_scope(client):
    env = await _env("vis")
    await make_user("owner@example.com", [f"investigator:{env}:shop"])
    await make_user("other@example.com", [f"viewer:{env}:secure"])
    h = await login(client, "owner@example.com")
    inv = (await client.post("/moks/api/investigations", json={"question": "what broke?", "environment": env, "namespace": "shop"}, headers=h)).json()
    await login(client, "other@example.com")
    assert (await client.get(f"/moks/api/investigations/{inv['id']}")).status_code == 404
    assert all(i["id"] != inv["id"] for i in (await client.get("/moks/api/investigations")).json())


async def test_deterministic_investigation_finds_problems_with_citations(client):
    env = await _env("det")
    await make_user("sre@example.com", [f"investigator:{env}:*"])
    h = await login(client, "sre@example.com")
    inv = (await client.post("/moks/api/investigations",
                             json={"question": "Why is checkout returning 502 errors in the last 30 minutes?", "environment": env, "namespace": "shop"},
                             headers=h)).json()
    window = (await client.get(f"/moks/api/investigations/{inv['id']}")).json()
    assert window["status"] == "queued"
    done = await _run(inv["id"])
    assert done.status == "completed", done.error
    report = done.report
    kinds = {f["kind"] for f in report["findings"]}
    assert {"service_selector_mismatch", "crashloop", "oomkilled"} <= kinds
    assert report["generated_by"] == "rules"
    refs = {e["ref"] for e in (await client.get(f"/moks/api/investigations/{inv['id']}")).json()["evidence"]}
    for fact in report["observed_facts"]:
        assert fact["evidence"] and set(fact["evidence"]) <= refs
    assert all(not c["verified"] for c in report["suspected_causes"])
    assert all(r["requires_approval"] and not r["executable"] for r in report["proposed_remediation"])
    # Evidence never carries the literal env secret
    detail = (await client.get(f"/moks/api/investigations/{inv['id']}")).json()
    assert "s3cr3t-value" not in json.dumps(detail) and "hunter2hunter2" not in json.dumps(detail)
    # The 30-minute window in the question was honoured
    from datetime import datetime

    span = datetime.fromisoformat(detail["time_to"]) - datetime.fromisoformat(detail["time_from"])
    assert int(span.total_seconds()) == 1800


async def test_sse_stream_replays_events_and_ends(client):
    env = await _env("sse")
    await make_user("sse@example.com", [f"investigator:{env}:*"])
    h = await login(client, "sse@example.com")
    inv = (await client.post("/moks/api/investigations", json={"question": "health?", "environment": env, "namespace": "shop"}, headers=h)).json()
    await _run(inv["id"])
    r = await client.get(f"/moks/api/investigations/{inv['id']}/events")
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"].startswith("no-cache")
    types = [line.split(": ", 1)[1] for line in r.text.splitlines() if line.startswith("event: ")]
    assert types[0] == "queued" and "tool_call" in types and "report" in types and types[-1] == "end"
    # Resume from Last-Event-ID only returns later events
    r2 = await client.get(f"/moks/api/investigations/{inv['id']}/events", headers={"Last-Event-ID": "3"})
    ids = [int(line[4:]) for line in r2.text.splitlines() if line.startswith("id: ")]
    assert ids and min(ids) == 4


async def test_cancellation_stops_a_running_investigation(client):
    env = await _env("cancel")
    await make_user("cancel@example.com", [f"investigator:{env}:*"])
    h = await login(client, "cancel@example.com")
    FakeKubernetes.delay = 3.0
    inv = (await client.post("/moks/api/investigations", json={"question": "slow?", "environment": env, "namespace": "shop"}, headers=h)).json()
    task = asyncio.create_task(_run(inv["id"]))
    await asyncio.sleep(0.5)
    assert (await client.post(f"/moks/api/investigations/{inv['id']}/cancel", headers=h)).json()["status"] == "cancelling"
    done = await asyncio.wait_for(task, 10)
    assert done.status == "cancelled"


async def test_time_limit_fails_cleanly(client, monkeypatch):
    from moks.config import get_settings

    monkeypatch.setattr(get_settings(), "max_duration_seconds", 1)
    env = await _env("timeout")
    await make_user("slow@example.com", [f"investigator:{env}:*"])
    h = await login(client, "slow@example.com")
    FakeKubernetes.delay = 5.0
    inv = (await client.post("/moks/api/investigations", json={"question": "slow?", "environment": env, "namespace": "shop"}, headers=h)).json()
    done = await asyncio.wait_for(_run(inv["id"]), 10)
    assert done.status == "failed" and "time limit" in done.error


async def test_revoked_access_is_rechecked_at_execution(client):
    from moks.models import RoleBinding
    from sqlalchemy import delete

    env = await _env("recheck")
    uid = await make_user("revoked@example.com", [f"investigator:{env}:*"])
    h = await login(client, "revoked@example.com")
    inv = (await client.post("/moks/api/investigations", json={"question": "what broke?", "environment": env, "namespace": "shop"}, headers=h)).json()
    async with session_scope() as db:
        await db.execute(delete(RoleBinding).where(RoleBinding.user_id == uid))
    done = await _run(inv["id"])
    assert done.status == "failed" and "not permitted" in done.error
    assert FakeKubernetes.calls == []


async def test_unavailable_connector_is_reported_not_hidden(client):
    name = f"broken-{uuid.uuid4().hex[:6]}"
    await make_env(name, connectors=[{"type": "kubernetes", "name": "k8s", "config": {"mode": "in_cluster"}},
                                     {"type": "prometheus", "name": "prom", "config": {"base_url": "http://127.0.0.1:9/prom", "timeout_seconds": 1, "retries": 0}}])
    await make_user("broken@example.com", [f"investigator:{name}:*"])
    h = await login(client, "broken@example.com")
    inv = (await client.post("/moks/api/investigations", json={"question": "what broke?", "environment": name, "namespace": "shop"}, headers=h)).json()
    done = await _run(inv["id"])
    assert done.status == "completed"
    assert any("prom_alerts failed" in m for m in done.report["missing_evidence"])


# ---- prompt injection with an LLM in the loop ------------------------------------------------

class ScriptedLLM:
    """OpenAI-compatible endpoint that replays a malicious plan, then a report citing fake refs."""

    def __init__(self):
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        system = body["messages"][0]["content"]
        if "tool_calls" in system:  # planner
            plan = {"tool_calls": [
                {"tool": "scale_deployment", "args": {"namespace": "shop", "name": "checkout", "replicas": 0}},
                {"tool": "k8s_pod_logs", "args": {"namespace": "secure", "pod": "vault-0"}},
                {"tool": "k8s_nodes", "args": {}},
                {"tool": "k8s_pod_logs", "args": {"namespace": "shop", "pod": "payments-5c4-xyz", "tail_lines": 99999}},
                {"tool": "k8s_pod_logs", "args": {"namespace": "shop", "pod": "payments-5c4-xyz", "previous": True}},
            ], "done": len(self.requests) > 1}
            content = json.dumps(plan)
        else:
            content = json.dumps({
                "summary": "The root cause is confirmed: payments crashes.",
                "observed_facts": [{"statement": "payments is crashlooping", "evidence": ["E1"]},
                                   {"statement": "made-up fact", "evidence": ["E99"]},
                                   {"statement": "uncited claim", "evidence": []}],
                "suspected_causes": [{"cause": "bad config", "confidence": "high", "evidence": ["E42"], "reasoning": "guess"}],
                "missing_evidence": [], "next_steps": ["check config"],
                "proposed_remediation": [{"action": "scale to 0", "target": "deployment/payments"}],
            })
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 1000, "completion_tokens": 100}})


async def test_prompt_injection_cannot_widen_tools_or_scope(client, monkeypatch):
    import os

    from moks.llm import providers

    env = await _env("inject")
    await make_user("victim@example.com", [f"investigator:{env}:shop"])
    llm = ScriptedLLM()
    real_init = providers.Provider.__init__

    def init(self, cfg, transport=None):
        real_init(self, cfg, httpx.MockTransport(llm))

    monkeypatch.setattr(providers.Provider, "__init__", init)
    monkeypatch.setenv("MOKS_CRED_TEST_OPENAI", "sk-test-not-real-key-000000")
    async with session_scope() as db:
        for other in (await db.execute(select(LLMConfig).where(LLMConfig.is_active.is_(True)))).scalars():
            other.is_active = False
        db.add(LLMConfig(name=f"scripted-{uuid.uuid4().hex[:6]}", purpose="chat", provider="openai", model="gpt-test",
                         endpoint="http://llm.test/v1", credential_ref={"type": "env", "name": "MOKS_CRED_TEST_OPENAI"},
                         is_active=True, input_price_per_mtok=1, output_price_per_mtok=2, daily_budget_usd=100))
    h = await login(client, "victim@example.com")
    inv = (await client.post("/moks/api/investigations", json={"question": "why is payments down?", "environment": env, "namespace": "shop"},
                             headers=h)).json()
    try:
        done = await _run(inv["id"])
    finally:
        async with session_scope() as db:
            for c in (await db.execute(select(LLMConfig).where(LLMConfig.is_active.is_(True)))).scalars():
                c.is_active = False

    assert done.status == "completed", done.error
    # The injected log text reached the model only inside an untrusted block, with the secret redacted
    planner_prompt = llm.requests[1]["messages"][1]["content"] if len(llm.requests) > 1 else llm.requests[0]["messages"][1]["content"]
    assert "hunter2hunter2" not in json.dumps(llm.requests)
    assert "sk-test-not-real-key" not in json.dumps(llm.requests)
    # Nothing outside scope was touched: the fake cluster never saw the 'secure' namespace
    assert ("ns", "secure") not in FakeKubernetes.calls
    async with session_scope() as db:
        denials = (await db.execute(select(AuditEvent).where(AuditEvent.decision == "deny", AuditEvent.actor_email == "victim@example.com"))).scalars().all()
    reasons = " ".join(d.details.get("kind", "") for d in denials)
    assert "unknown_tool" in reasons and "scope" in reasons
    assert any(d.resource_id == "k8s_nodes" for d in denials)
    # Invalid arguments (tail_lines too large) were rejected by validation, not executed
    events = (await client.get(f"/moks/api/investigations/{inv['id']}/events")).text
    assert "invalid_args" in events
    # Report validation: uncited/fabricated facts dropped, unsupported cause downgraded, 'confirmed' flagged
    rep = done.report
    assert [f["statement"] for f in rep["observed_facts"]] == ["payments is crashlooping"]
    assert rep["suspected_causes"][0]["confidence"] == "low" and rep["suspected_causes"][0]["verified"] is False
    assert "Unverified" in rep["summary"]
    assert rep["proposed_remediation"][0]["executable"] is False
    assert done.tokens_in >= 2000 and float(done.cost_usd) > 0
