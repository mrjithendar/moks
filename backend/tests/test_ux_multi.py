"""Multi-environment views, auto-scoped investigations, audit filters, FinOps overview and LLM spend."""
import uuid

import pytest

from moks.connectors import registry
from moks.db import session_scope
from moks.models import Environment, Investigation
from moks.orchestrator.engine import Investigator
from tests.conftest import login, make_env, make_user
from tests.fakes import FakeKubernetes


@pytest.fixture(autouse=True)
def fake_k8s(monkeypatch):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "kubernetes", FakeKubernetes)
    registry._cache.clear()
    yield
    registry._cache.clear()


async def _two_envs():
    tag = uuid.uuid4().hex[:6]
    a, b = f"ua-{tag}", f"ub-{tag}"
    for n in (a, b):
        await make_env(n, connectors=[{"type": "kubernetes", "name": "k8s", "config": {}}])
    return tag, a, b


async def test_pods_across_all_environments_and_namespaces(client):
    tag, a, b = await _two_envs()
    await make_user(f"wide-{tag}@example.com", [f"investigator:{a}:*", f"investigator:{b}:shop"])
    await login(client, f"wide-{tag}@example.com")
    r = (await client.get("/moks/api/multi/pods", params={"environment": "all", "namespace": "all"})).json()
    envs = {(p["environment"], p["namespace"]) for p in r["items"]}
    assert (a, "shop") in envs and (a, "secure") in envs and (b, "shop") in envs
    assert (b, "secure") not in envs  # scoped user: other namespace in env b is never queried
    one = (await client.get("/moks/api/multi/pods", params={"environment": b, "namespace": "shop"})).json()
    assert {p["environment"] for p in one["items"]} == {b}


async def test_investigation_without_environment_or_namespace(client):
    tag, a, b = await _two_envs()
    async with session_scope() as db:
        from sqlalchemy import select

        env = (await db.execute(select(Environment).where(Environment.name == b))).scalar_one()
        env.is_default = True
    await make_user(f"auto-{tag}@example.com", [f"investigator:{a}:*", f"investigator:{b}:*"])
    h = await login(client, f"auto-{tag}@example.com")
    named = (await client.post("/moks/api/investigations", json={"question": f"why are payments pods crashing in {a}?"}, headers=h)).json()
    assert named["environment"] == a and named["namespace"] == "*"
    default = (await client.post("/moks/api/investigations", json={"question": "why is payments crashing?"}, headers=h)).json()
    assert default["environment"] == b
    await Investigator(uuid.UUID(default["id"]), "t").run()
    async with session_scope() as db:
        inv = await db.get(Investigation, uuid.UUID(default["id"]))
    assert inv.status == "completed" and inv.namespace == "shop"  # narrowed via the 'payments' workload in shop
    assert any("payments" in n for n in inv.report["notes"])
    assert "crashloop" in {f["kind"] for f in inv.report["findings"]}


async def test_scoped_user_investigation_uses_their_namespace(client):
    tag, a, b = await _two_envs()
    await make_user(f"one-{tag}@example.com", [f"investigator:{a}:shop"])
    h = await login(client, f"one-{tag}@example.com")
    r = (await client.post("/moks/api/investigations", json={"question": "anything wrong?"}, headers=h)).json()
    assert r["environment"] == a and r["namespace"] == "shop"


async def test_audit_filters_and_facets(client):
    tag = uuid.uuid4().hex[:6]
    await make_user(f"aud-{tag}@example.com", ["admin"])
    await login(client, f"aud-{tag}@example.com")
    r = (await client.get("/moks/api/admin/audit", params={"actor": f"aud-{tag}", "action": "auth.*", "q_text": "dev"})).json()
    assert r["events"] and all(e["action"].startswith("auth.") for e in r["events"])
    assert (await client.get("/moks/api/admin/audit", params={"since": "not-a-date"})).status_code == 422
    f = (await client.get("/moks/api/admin/audit/facets")).json()
    assert "auth.login" in f["action"]


async def test_finops_overview_and_llm_spend(client):
    tag = uuid.uuid4().hex[:6]
    name = f"fo-{tag}"
    await make_env(name, connectors=[{"type": "kubernetes", "name": "k8s", "config": {}}])
    await make_user(f"fo-{tag}@example.com", [f"investigator:{name}:*"])
    await login(client, f"fo-{tag}@example.com")
    r = (await client.get("/moks/api/finops/overview", params={"environment": name})).json()
    assert r["environments"][0]["errors"]["kubernetes"].startswith("needs") and set(r["totals"]) >= {"cpu", "memory", "storage", "aws", "llm", "all"}
    llm = (await client.get("/moks/api/finops/llm")).json()
    assert "by_model" in llm and "by_environment" in llm


def test_loki_logs_builds_scoped_query():
    from moks.tools.builtin import LokiLogsArgs

    a = LokiLogsArgs(level="error", contains="timeout")
    assert a.namespace is None and a.level == "error"


async def test_partial_outage_is_reported_per_namespace_not_a_500(client, monkeypatch):
    """Connector errors audit on the shared session while the other namespaces are still in flight."""
    tag, a, _ = await _two_envs()
    await make_user(f"out-{tag}@example.com", [f"investigator:{a}:*"])
    await login(client, f"out-{tag}@example.com")

    async def boom(self, namespace, label_selector=None):
        from moks.connectors.base import ConnectorError

        if namespace == "secure":
            raise ConnectorError("upstream", "api server timed out")
        return await FakeKubernetes.original_list_pods(self, namespace, label_selector)

    monkeypatch.setattr(FakeKubernetes, "original_list_pods", FakeKubernetes.list_pods, raising=False)
    monkeypatch.setattr(FakeKubernetes, "list_pods", boom)
    r = await client.get("/moks/api/multi/pods", params={"environment": a})
    assert r.status_code == 200
    body = r.json()
    assert any(p["namespace"] == "shop" for p in body["items"])
    assert "secure" in [e["namespace"] for e in body["errors"]]


async def test_scopes_for_filter_dropdowns(client):
    tag, a, b = await _two_envs()
    await make_user(f"sc-{tag}@example.com", [f"investigator:{a}:*", f"investigator:{b}:shop"])
    await login(client, f"sc-{tag}@example.com")
    rows = {s["environment"]: s["namespaces"] for s in (await client.get("/moks/api/multi/scopes")).json()["scopes"]}
    assert "secure" in rows[a] and rows[b] == ["shop"]


async def test_finops_overview_skips_a_failing_namespace(client, monkeypatch):
    tag = uuid.uuid4().hex[:6]
    name = f"fs-{tag}"
    await make_env(name, connectors=[{"type": "kubernetes", "name": "k8s", "config": {}}, {"type": "prometheus", "name": "p", "config": {}},
                                     {"type": "cost_model", "name": "c", "config": {"monthly_cost_usd": 730, "cpu_share": 0.5}}])
    await make_user(f"fs-{tag}@example.com", [f"investigator:{name}:*"])
    await login(client, f"fs-{tag}@example.com")

    class Prom:
        type = "prometheus"

        def __init__(self, config, credential_ref=None):
            pass

        async def aclose(self):
            pass

        async def instant(self, q, at):
            return {"resultType": "vector", "series": []}

    monkeypatch.setitem(registry.CONNECTOR_TYPES, "prometheus", Prom)
    r = (await client.get("/moks/api/finops/overview", params={"environment": name})).json()
    env = r["environments"][0]
    assert env["errors"]["skipped namespaces"] == "kube-system"  # the fake lists it but cannot read it
    assert {w["namespace"] for w in env["workloads"]} == {"shop", "secure"} and "rightsizing" in env
    assert r["totals"]["kubernetes"] >= 0
