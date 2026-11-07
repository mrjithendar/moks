"""The opt-in CrewAI workload investigation (docs/crewai-investigation.md), the parts that run without the
crewai package: the flag off, the evidence registry's validation and the tool binding. The crew end to end
is in test_crewai_crew.py, which needs the crewai extra.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, update

from moks import crew, versions
from moks.config import get_settings
from moks.connectors import registry
from moks.crew import capability
from moks.crew.evidence import Registry, validate
from moks.crew.tools import ALLOWED_MOKS_TOOLS, BY_NAME, Empty, Members, Refused, Target, bind, focus, mine, owned_by
from moks.db import session_scope
from moks.models import ComponentVersion, Investigation, InvestigationEvent, LLMConfig
from moks.orchestrator import worker
from moks.orchestrator.engine import Investigator
from tests.conftest import login, make_env, make_user
from tests.fakes import FakeKubernetes

MODEL = SimpleNamespace(provider="openai", model="m")
TARGET = Target(environment="e", namespace="shop", workload="payments", kind="Deployment", minutes=60, repository="")


def _record(ref: str, tool: str = "k8s_list_pods", ok: bool = True, kind: str = "observation") -> dict:
    return {"ref": ref, "tool": tool, "args": {"namespace": "shop"}, "ok": ok, "summary": f"{tool} result", "data": [{"name": "payments-5c4-xyz"}],
            "error": None if ok else "timeout", "collected_at": "2026-11-06T10:00:00Z", "truncated": False, "kind": kind}


def test_capability_is_off_by_default_and_says_why():
    state = capability(get_settings(), None)
    assert state["enabled"] is False and state["available"] is False
    assert "MOKS_CREWAI_ENABLED" in state["reason"]


def test_the_flag_alone_does_not_make_it_available(monkeypatch):
    monkeypatch.setattr(get_settings(), "crewai_enabled", True)
    monkeypatch.setattr("moks.crew.installed_version", lambda: None)
    state = capability(get_settings(), object())
    assert state["available"] is False and "not installed" in state["reason"]


async def test_the_api_needs_a_running_crewai_worker(monkeypatch):
    monkeypatch.setattr(get_settings(), "crewai_enabled", True)
    async with session_scope() as db:
        await db.execute(delete(ComponentVersion).where(ComponentVersion.component == crew.WORKER))
    async with session_scope() as db:
        state = capability(get_settings(), MODEL, await crew.worker_version(db))
    assert state["available"] is False and "no CrewAI worker is running" in state["reason"]
    await versions.report(crew.WORKER, crew.PINNED)  # what the CrewAI worker's heartbeat writes
    async with session_scope() as db:
        state = capability(get_settings(), MODEL, await crew.worker_version(db))
    assert state["available"] is True and state["version"] == crew.PINNED
    async with session_scope() as db:
        await db.execute(delete(ComponentVersion).where(ComponentVersion.component == crew.WORKER))


def test_only_a_worker_with_the_class_and_the_package_takes_crews(monkeypatch):
    assert worker.crewai_version({"detection", "interactive", "knowledge", "maintenance"}) is None
    monkeypatch.setattr("moks.crew.installed_version", lambda: None)
    assert worker.crewai_version({"crewai"}) is None  # the default image: it logs and takes none
    monkeypatch.setattr("moks.crew.installed_version", lambda: crew.PINNED)
    assert worker.crewai_version({"crewai"}) == crew.PINNED


async def test_a_crewai_investigation_goes_only_to_the_crewai_worker(client):
    async with session_scope() as db:
        await db.execute(update(Investigation).where(Investigation.status == "queued").values(status="cancelled"))
    env = f"cr-{uuid.uuid4().hex[:6]}"
    await make_env(env, connectors=[{"type": "kubernetes", "name": "k", "config": {}}])
    await make_user(f"{env}@example.com", [f"investigator:{env}:*"])
    h = await login(client, f"{env}@example.com")
    plain, crewai = [(await client.post("/moks/api/investigations", json={"question": "why is payments crashing", "environment": env, "namespace": "shop"}, headers=h)).json()["id"]
                     for _ in range(2)]
    async with session_scope() as db:
        queued = (await db.execute(select(InvestigationEvent).where(InvestigationEvent.investigation_id == uuid.UUID(crewai), InvestigationEvent.seq == 0))).scalar_one()
        queued.payload = {**queued.payload, "crewai": {"workload": "payments", "kind": "Deployment"}}  # what POST /investigations/crewai stores
    assert str((await worker.claim("crewai-worker", crewai=True)).id) == crewai
    assert str((await worker.claim("default-worker")).id) == plain
    assert await worker.claim("default-worker") is None and await worker.claim("crewai-worker", crewai=True) is None


async def test_flag_off_hides_the_route_and_reports_unavailable(client):
    tag = uuid.uuid4().hex[:6]
    env = f"cr-{tag}"
    await make_env(env, connectors=[{"type": "kubernetes", "name": "k", "config": {}}])
    await make_user(f"{env}@example.com", [f"investigator:{env}:*"])
    h = await login(client, f"{env}@example.com")
    cfg = (await client.get("/moks/api/investigate/config")).json()
    assert cfg["crewai"]["available"] is False and cfg["crewai"]["enabled"] is False
    r = await client.post("/moks/api/investigations/crewai", json={"environment": env, "namespace": "shop", "workload": "payments"}, headers=h)
    assert r.status_code == 404


async def test_flag_off_leaves_the_default_investigation_alone(client, monkeypatch):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "kubernetes", FakeKubernetes)
    registry._cache.clear()
    async with session_scope() as db:
        for c in (await db.execute(select(LLMConfig).where(LLMConfig.purpose == "chat"))).scalars():
            c.is_active = False
    env = f"cr-{uuid.uuid4().hex[:6]}"
    await make_env(env, connectors=[{"type": "kubernetes", "name": "k", "config": {}}])
    await make_user(f"{env}@example.com", [f"investigator:{env}:*"])
    h = await login(client, f"{env}@example.com")
    iid = (await client.post("/moks/api/investigations", json={"question": "why is payments crashing", "environment": env, "namespace": "shop"}, headers=h)).json()["id"]
    await Investigator(uuid.UUID(iid), "t").run()
    async with session_scope() as db:
        inv = await db.get(Investigation, uuid.UUID(iid))
    assert inv.status == "completed" and "crewai" not in inv.report and inv.report["generated_by"] == "rules"
    registry._cache.clear()


def test_an_invented_citation_is_rejected_and_an_uncited_claim_removed():
    reg = Registry()
    reg.add(_record("E1"), agent="workload")
    reg.add(_record("E2", tool="k8s_events"), agent="workload")
    raw = {"summary": "payments crash-loops",
           "findings": [{"statement": "payments-5c4-xyz is in CrashLoopBackOff", "evidence": ["E1", "E99"]},
                        {"statement": "the database is down", "evidence": ["E42"]},
                        {"statement": "nothing cited", "evidence": []}],
           "likely_causes": [{"cause": "a bad image", "confidence": "high", "evidence": ["E1"]}, {"cause": "made up", "confidence": "high", "evidence": ["E7"]}],
           "missing_evidence": [], "next_steps": ["read previous logs"]}
    report, decided = validate(raw, reg, [])
    assert [f.evidence for f in report.observed_facts] == [["E1"]]
    assert set(decided["citations_rejected"]) == {"E99", "E42", "E7"}
    assert {r["statement"] for r in decided["claims_removed"]} == {"the database is down", "nothing cited", "made up"}
    assert all(set(c.evidence) <= set(reg.records) for c in report.suspected_causes)
    # one live source cannot carry high confidence
    assert report.suspected_causes[0].confidence == "medium" and decided["confidence_lowered"]


def test_confidence_with_only_failed_evidence_is_low():
    reg = Registry()
    reg.add(_record("E1", ok=False, kind="tool_error"), agent="workload")
    report, _ = validate({"likely_causes": [{"cause": "x", "confidence": "high", "evidence": ["E1"]}]}, reg, [])
    assert report.suspected_causes[0].confidence == "low"
    assert any("timeout" in g["reason"] for g in reg.gaps)


def test_model_quotes_never_become_excerpts():
    reg = Registry()
    reg.add(_record("E1"), agent="workload")
    report, _ = validate({"findings": [{"statement": "s", "evidence": ["E1"], "excerpt": "FORGED password=x"}]}, reg, [])
    assert "FORGED" not in json.dumps(report.model_dump()) and "FORGED" not in json.dumps(reg.records)


PAYMENTS = Members(frozenset({"payments-5c4-xyz"}), frozenset({("ReplicaSet", "payments-5c4", None)}))


def test_the_target_is_bound_by_moks_not_the_model():
    args = bind(BY_NAME["pod_logs"], TARGET, {"pod": "payments-5c4-xyz", "previous": True}, log_lines=100, members=PAYMENTS)
    assert args["namespace"] == "shop" and args["tail_lines"] == 100
    with pytest.raises(Refused):
        bind(BY_NAME["pod_logs"], TARGET, {"pod": "payments-5c4-xyz", "namespace": "kube-system"}, log_lines=100, members=PAYMENTS)
    with pytest.raises(Refused):
        bind(BY_NAME["describe_pod"], TARGET, {"pod": "vault-0"}, log_lines=100, members=PAYMENTS)
    with pytest.raises(LookupError):
        bind(BY_NAME["recent_commits"], TARGET, {}, log_lines=100)  # no repository configured: a gap, not a guess


def test_only_read_only_tools_are_on_the_list():
    from moks.tools.builtin import REGISTRY

    assert ALLOWED_MOKS_TOOLS <= set(REGISTRY)
    assert all(REGISTRY[t].read_only and not REGISTRY[t].privileged for t in ALLOWED_MOKS_TOOLS)
    assert not ALLOWED_MOKS_TOOLS & {"exec_command", "k8s_exec", "scale_deployment", "restart_deployment", "delete_pod", "kubectl_apply"}


# ---- workload membership by owner reference (review fix) ------------------------------------------------------

CART = Target(environment="e", namespace="shop", workload="cart", kind="Deployment", minutes=60, repository="")
CART_RS = frozenset({("ReplicaSet", "cart-7d9f", "rs-cart")})
CART_PODS = [{"name": "cart-7d9f-abc", "owner": "ReplicaSet/cart-7d9f", "ownerUid": "rs-cart"},
             {"name": "cart-worker-6c8-xyz", "owner": "ReplicaSet/cart-worker-6c8", "ownerUid": "rs-worker"},  # same prefix, another Deployment
             {"name": "cart-7d9f-old", "owner": "ReplicaSet/cart-7d9f", "ownerUid": "rs-before-recreation"},  # an RS of that name the old Deployment owned
             {"name": "cart-7d9f-orphan", "owner": None, "ownerUid": None}]  # no owner references


def test_pods_belong_to_a_workload_by_owner_reference_not_by_name():
    members = Members(frozenset(p["name"] for p in CART_PODS if owned_by(p, CART_RS)), CART_RS)
    assert members.pods == {"cart-7d9f-abc"}
    for pod in ("cart-worker-6c8-xyz", "cart-7d9f-old", "cart-7d9f-orphan"):
        with pytest.raises(Refused):
            bind(BY_NAME["describe_pod"], CART, {"pod": pod}, log_lines=10, members=members)
    assert bind(BY_NAME["search_logs"], CART, {}, log_lines=10, members=members)["pod"] == "(cart-7d9f-abc)"
    assert bind(BY_NAME["pod_metrics"], CART, {}, log_lines=10, members=members)["pod_regex"] == "(cart-7d9f-abc)"
    assert bind(BY_NAME["recent_events"], CART, {"object_name": "cart"}, log_lines=10, members=members)["object_name"] == "cart"
    with pytest.raises(Refused):
        bind(BY_NAME["recent_events"], CART, {"object_name": "cart-worker"}, log_lines=10, members=members)


def test_namespace_wide_results_narrow_to_the_workload_and_to_nothing_when_nothing_matches():
    members = Members(frozenset({"cart-7d9f-abc"}), CART_RS)
    assert [p["name"] for p in focus(CART, "k8s_list_pods", CART_PODS, members)] == ["cart-7d9f-abc"]
    workloads = [{"kind": "Deployment", "name": "cart"}, {"kind": "Deployment", "name": "cart-worker"}]
    assert focus(CART, "k8s_workloads", workloads, members) == [{"kind": "Deployment", "name": "cart"}]
    triage = {"findings": [{"kind": "crash", "resources": ["pod/cart-worker-6c8-xyz"]}, {"kind": "oom", "resources": ["pod/cart-7d9f-abc"]}],
              "unhealthyPods": CART_PODS, "workloads": workloads,
              "warningEvents": [{"object": "Pod/cart-worker-6c8-xyz"}, {"object": "Pod/cart-7d9f-abc"}, {"object": "Deployment/cart"}]}
    got = focus(CART, "k8s_triage", triage, members)
    assert [f["kind"] for f in got["findings"]] == ["oom"] and [p["name"] for p in got["unhealthyPods"]] == ["cart-7d9f-abc"]
    assert got["warningEvents"] == [{"object": "Pod/cart-7d9f-abc"}, {"object": "Deployment/cart"}]
    audit = {"workloads": [{"workload": "Deployment/cart"}, {"workload": "Deployment/cart-worker"}]}
    assert focus(CART, "k8s_config_audit", audit, members)["workloads"] == [{"workload": "Deployment/cart"}]
    assert not mine(CART, {"resources": ["pod/cart-worker-6c8-xyz"]}, members) and mine(CART, {"resources": ["deployment/cart"]}, members)

    none = Members(frozenset(), CART_RS, "no pod's owner references point at it")
    assert focus(CART, "k8s_list_pods", CART_PODS, none) == []  # an explicit empty result, never the whole namespace
    assert focus(CART, "k8s_triage", triage, none)["unhealthyPods"] == []
    for tool in ("search_logs", "pod_metrics"):
        with pytest.raises(Empty):
            bind(BY_NAME[tool], CART, {}, log_lines=10, members=none)
