"""The opt-in CrewAI workload investigation (docs/crewai-investigation.md), the parts that run without the
crewai package: the flag off, the evidence registry's validation and the tool binding. The crew end to end
is in test_crewai_crew.py, which needs the crewai extra.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from moks.config import get_settings
from moks.connectors import registry
from moks.crew import capability
from moks.crew.evidence import Registry, validate
from moks.crew.tools import ALLOWED_MOKS_TOOLS, BY_NAME, Refused, Target, bind
from moks.db import session_scope
from moks.models import Investigation, LLMConfig
from moks.orchestrator.engine import Investigator
from tests.conftest import login, make_env, make_user
from tests.fakes import FakeKubernetes

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


def test_the_target_is_bound_by_moks_not_the_model():
    args = bind(BY_NAME["pod_logs"], TARGET, {"pod": "payments-5c4-xyz", "previous": True}, log_lines=100)
    assert args["namespace"] == "shop" and args["tail_lines"] == 100
    with pytest.raises(Refused):
        bind(BY_NAME["pod_logs"], TARGET, {"pod": "payments-5c4-xyz", "namespace": "kube-system"}, log_lines=100)
    with pytest.raises(Refused):
        bind(BY_NAME["describe_pod"], TARGET, {"pod": "vault-0"}, log_lines=100)
    with pytest.raises(LookupError):
        bind(BY_NAME["recent_commits"], TARGET, {}, log_lines=100)  # no repository configured: a gap, not a guess


def test_only_read_only_tools_are_on_the_list():
    from moks.tools.builtin import REGISTRY

    assert ALLOWED_MOKS_TOOLS <= set(REGISTRY)
    assert all(REGISTRY[t].read_only and not REGISTRY[t].privileged for t in ALLOWED_MOKS_TOOLS)
    assert not ALLOWED_MOKS_TOOLS & {"exec_command", "k8s_exec", "scale_deployment", "restart_deployment", "delete_pod", "kubectl_apply"}
