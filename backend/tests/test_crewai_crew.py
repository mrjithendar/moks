"""The CrewAI workload investigation end to end (docs/crewai-investigation.md): a scripted model answers
through llm.budget and FakeKubernetes serves canned state, so nothing reaches a network. Needs the crewai
extra (backend/requirements-crewai.txt, Python 3.10 to 3.13); skipped where it is not installed.
"""
from __future__ import annotations

import asyncio
import json
import socket
import uuid

import pytest

crewai = pytest.importorskip("crewai", reason="the crewai extra is not installed (backend/requirements-crewai.txt)")

from sqlalchemy import select  # noqa: E402

from moks.config import get_settings  # noqa: E402
from moks.connectors import registry  # noqa: E402
from moks.crew.tools import ALLOWED_MOKS_TOOLS  # noqa: E402
from moks.db import session_scope  # noqa: E402
from moks.llm import budget  # noqa: E402
from moks.llm.providers import LLMResponse  # noqa: E402
from moks.models import AuditEvent, Evidence, Investigation, LLMConfig  # noqa: E402
from moks.orchestrator import engine  # noqa: E402
from moks.orchestrator.engine import Investigator  # noqa: E402
from tests.conftest import login, make_env, make_user  # noqa: E402
from tests.fakes import FakeKubernetes  # noqa: E402


def act(tool: str, args: dict | None = None) -> str:
    return f"Thought: I need {tool}.\nAction: {tool}\nAction Input: {json.dumps(args or {})}"


def final(text: str) -> str:
    return f"Thought: I now know the final answer\nFinal Answer: {text}"


REPORT = json.dumps({
    "summary": "payments is crash-looping since its last rollout.",
    "findings": [{"statement": "Pod payments-5c4-xyz is in CrashLoopBackOff with 7 restarts", "evidence": ["E1"]},
                 {"statement": "Invented observation", "evidence": ["E99"]}],
    "likely_causes": [{"cause": "The application exits on start-up (db timeout in its logs)", "confidence": "high", "evidence": ["E1", "E2"], "reasoning": "logs and pod state"}],
    "missing_evidence": ["no metrics"], "next_steps": ["Read the previous container's logs in full"]})


class Script:
    """Replies per agent, chosen by the agent's role in the system prompt."""

    def __init__(self, **per_agent: list):
        self.replies = {k: list(v) for k, v in per_agent.items()}
        self.calls: list[tuple[str, str]] = []
        self.after_cancel = 0

    def agent(self, system: str) -> str:
        for key, role in (("workload", "Workload investigator"), ("change", "Change investigator"), ("review", "Incident reviewer")):
            if role in system:
                return key
        return "?"

    async def __call__(self, provider, system, messages, **kw):
        who = self.agent(system)
        self.calls.append((who, "\n".join(str(m["content"]) for m in messages)))
        queue = self.replies.get(who) or []
        reply = queue.pop(0) if queue else final("done")
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = await reply()
        return LLMResponse(reply, 10, 5, "fake", "m")


class Model:
    name = "fake"
    cfg = None


@pytest.fixture
async def crew_on(monkeypatch):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "kubernetes", FakeKubernetes)
    registry._cache.clear()
    s = get_settings()
    monkeypatch.setattr(s, "crewai_enabled", True)
    monkeypatch.setattr(s, "crewai_tool_timeout_seconds", 2)
    monkeypatch.setattr(engine, "provider_for", lambda cfg: Model())
    async with session_scope() as db:
        for c in (await db.execute(select(LLMConfig).where(LLMConfig.purpose == "chat"))).scalars():
            c.is_active = False
        await db.flush()
        cfg = LLMConfig(name=f"c-{uuid.uuid4().hex[:6]}", purpose="chat", provider="openai", model="m", is_active=True)
        db.add(cfg)
    yield monkeypatch
    async with session_scope() as db:
        (await db.get(LLMConfig, cfg.id)).is_active = False
    registry._cache.clear()


async def start(client, workload="payments", namespace="shop", grants=None):
    env = f"cw-{uuid.uuid4().hex[:6]}"
    await make_env(env, connectors=[{"type": "kubernetes", "name": "k", "config": {}}])
    await make_user(f"{env}@example.com", grants or [f"investigator:{env}:*"])
    h = await login(client, f"{env}@example.com")
    r = await client.post("/moks/api/investigations/crewai", json={"environment": env, "namespace": namespace, "workload": workload}, headers=h)
    return r, h


async def result(iid):
    async with session_scope() as db:
        inv = await db.get(Investigation, uuid.UUID(iid))
        ev = (await db.execute(select(Evidence).where(Evidence.investigation_id == inv.id))).scalars().all()
        return inv, {e.ref: e for e in ev}


def happy(script_report=REPORT) -> Script:
    return Script(workload=[act("namespace_health"), act("pod_logs", {"pod": "payments-5c4-xyz", "previous": True}), final("payments crash-loops [E1] [E2]")],
                  change=[act("rollout_history"), final("one rollout in the window [E3]")],
                  review=[act("evidence_list"), final(script_report)])


async def test_capability_reports_available_with_the_flag_and_a_model(client, crew_on):
    await start(client)
    cfg = (await client.get("/moks/api/investigate/config")).json()["crewai"]
    assert cfg["available"] is True and cfg["version"] == "1.15.36" and cfg["llm"] == "openai/m"


async def test_a_crew_report_cites_only_recorded_evidence(client, crew_on):
    script = happy()
    crew_on.setattr(budget, "complete", script)
    r, _ = await start(client)
    assert r.status_code == 201, r.text
    await Investigator(uuid.UUID(r.json()["id"]), "t").run()
    inv, ev = await result(r.json()["id"])
    assert inv.status == "completed", inv.error
    rep = inv.report
    crew = rep["crewai"]
    assert crew["status"] == "completed" and [a["status"] for a in crew["agents"]] == ["done", "done", "done"]
    assert {e["source"] for e in crew["registry"]} == {"k8s_triage", "k8s_pod_logs", "k8s_rollout_history"}
    cited = {r for f in rep["observed_facts"] for r in f["evidence"]} | {r for c in rep["suspected_causes"] for r in c["evidence"]}
    assert cited and cited <= set(ev)  # every citation links to a stored evidence row
    assert "E99" in crew["validation"]["citations_rejected"]
    assert all(f["statement"] != "Invented observation" for f in rep["observed_facts"])
    # excerpts come from the stored, redacted evidence
    for e in crew["evidence"]:
        assert e["id"] in ev and "hunter2hunter2" not in e["excerpt"]
    # one trace, and tokens were metered through llm.budget
    assert inv.tokens_in > 0 and len(script.calls) >= 6


async def test_another_namespace_is_refused_at_the_tool_layer(client, crew_on):
    script = Script(workload=[act("pod_logs", {"pod": "payments-5c4-xyz", "namespace": "secure"}), act("describe_pod", {"pod": "vault-0"}), final("x")],
                    review=[final(REPORT)])
    crew_on.setattr(budget, "complete", script)
    r, _ = await start(client)
    iid = r.json()["id"]
    await Investigator(uuid.UUID(iid), "t").run()
    inv, ev = await result(iid)
    assert inv.status == "completed"
    assert not any((e.args or {}).get("namespace") == "secure" for e in ev.values())
    assert {x["tool"] for x in inv.report["crewai"]["refused"]} == {"pod_logs", "describe_pod"}
    async with session_scope() as db:
        audits = (await db.execute(select(AuditEvent).where(AuditEvent.action == "tool.call", AuditEvent.outcome == "not_needed"))).scalars().all()
    assert any((a.details or {}).get("investigation_id") == iid and "crewai" in (a.details or {}).get("reason", "") for a in audits)


async def test_a_namespace_the_person_may_not_read_is_denied_by_the_executor(client, crew_on):
    script = happy()
    crew_on.setattr(budget, "complete", script)
    env = f"cw-{uuid.uuid4().hex[:6]}"
    await make_env(env, connectors=[{"type": "kubernetes", "name": "k", "config": {}}])
    await make_user(f"{env}@example.com", [f"investigator:{env}:secure"])
    h = await login(client, f"{env}@example.com")
    r = await client.post("/moks/api/investigations/crewai", json={"environment": env, "namespace": "shop", "workload": "payments"}, headers=h)
    assert r.status_code == 403  # refused before anything is queued

    # and when access is lost after queueing, the executor's own check refuses every call
    r2 = await client.post("/moks/api/investigations/crewai", json={"environment": env, "namespace": "secure", "workload": "vault"}, headers=h)
    iid = r2.json()["id"]
    async with session_scope() as db:
        inv = await db.get(Investigation, uuid.UUID(iid))
        inv.namespace = "shop"  # as if the request named a namespace outside the person's grants
    await Investigator(uuid.UUID(iid), "t").run()
    inv, ev = await result(iid)
    assert inv.status == "failed" and "not permitted" in inv.error and not ev


async def test_a_tool_timeout_gives_a_partial_report_with_the_gap(client, crew_on):
    async def slow_logs(self, *a, **k):
        await asyncio.sleep(5)
        return ""
    crew_on.setattr(FakeKubernetes, "pod_logs", slow_logs)
    crew_on.setattr(budget, "complete", happy())
    r, _ = await start(client)
    await Investigator(uuid.UUID(r.json()["id"]), "t").run()
    inv, ev = await result(r.json()["id"])
    assert inv.status == "completed"
    assert ev["E2"].ok is False and ev["E2"].error == "timeout"
    assert any(g["source"] == "k8s_pod_logs" and "timeout" in g["reason"] for g in inv.report["crewai"]["gaps"])
    assert any("k8s_pod_logs" in m for m in inv.report["missing_evidence"])


async def test_a_provider_failure_keeps_the_evidence(client, crew_on):
    from moks.llm.providers import LLMError

    crew_on.setattr(budget, "complete", Script(workload=[act("namespace_health"), LLMError("unavailable", "provider down")]))
    r, _ = await start(client)
    await Investigator(uuid.UUID(r.json()["id"]), "t").run()
    inv, ev = await result(r.json()["id"])
    assert inv.status == "completed" and "E1" in ev
    crew = inv.report["crewai"]
    assert crew["status"] == "partial" and crew["stopped"]
    assert inv.report["generated_by"] == "rules"  # MOKS's usual fallback over what was collected


async def test_the_tool_call_limit_holds(client, crew_on):
    crew_on.setattr(get_settings(), "crewai_max_tool_calls", 2)
    many = [act("namespace_health"), act("workload_status"), act("workload_pods"), act("recent_events"), final("done")]
    crew_on.setattr(budget, "complete", Script(workload=many, review=[final(REPORT)]))
    r, _ = await start(client)
    await Investigator(uuid.UUID(r.json()["id"]), "t").run()
    inv, ev = await result(r.json()["id"])
    assert inv.status == "completed" and len(ev) == 2
    assert any(g["source"] == "limits" for g in inv.report["crewai"]["gaps"])


async def test_the_iteration_limit_holds(client, crew_on):
    crew_on.setattr(get_settings(), "crewai_max_iterations", 2)
    endless = [act("recent_events", {"object_name": f"payments-{i}"}) for i in range(30)]
    script = Script(workload=endless, review=[final(REPORT)])
    crew_on.setattr(budget, "complete", script)
    r, _ = await start(client)
    await Investigator(uuid.UUID(r.json()["id"]), "t").run()
    inv, _ = await result(r.json()["id"])
    assert inv.status == "completed"
    assert sum(1 for who, _ in script.calls if who == "workload") <= 2 + 2


async def test_cancellation_reaches_the_crew(client, crew_on):
    cancelled = asyncio.Event()
    script = Script()

    async def cancel_then_wait():
        async with session_scope() as db:
            (await db.get(Investigation, iid)).cancel_requested = True
        await asyncio.sleep(3)  # the watcher cancels the run while this call is in flight
        cancelled.set()
        return act("workload_status")
    script.replies["workload"] = [act("namespace_health"), cancel_then_wait, act("workload_pods"), final("x")]
    crew_on.setattr(budget, "complete", script)
    r, _ = await start(client)
    iid = uuid.UUID(r.json()["id"])
    await Investigator(iid, "t").run()
    inv, ev = await result(str(iid))
    assert inv.status == "cancelled"
    assert list(ev) == ["E1"]  # kept, and nothing ran after the cancel
    await asyncio.sleep(0.5)
    assert sum(1 for who, _ in script.calls if who == "workload") == 2


async def test_instructions_in_a_log_line_change_nothing(client, crew_on):
    """FakeKubernetes's logs for shop carry an injection (tests/fakes.py INJECTION). The scripted model
    obeys it, as a compromised model would: the calls it asks for do not exist or are refused."""
    script = Script(workload=[act("pod_logs", {"pod": "payments-5c4-xyz", "previous": True}),
                              act("scale_deployment", {"replicas": 0}),
                              act("pod_logs", {"pod": "coredns-1", "namespace": "kube-system"}),
                              act("recent_events", {"namespace": "kube-system"}), final("done")],
                    review=[final(REPORT)])
    crew_on.setattr(budget, "complete", script)
    r, _ = await start(client)
    iid = r.json()["id"]
    await Investigator(uuid.UUID(iid), "t").run()
    inv, ev = await result(iid)
    assert inv.status == "completed"
    assert {e.tool for e in ev.values()} <= ALLOWED_MOKS_TOOLS
    assert all((e.args or {}).get("namespace") in (None, "shop") for e in ev.values())
    assert [e.tool for e in ev.values()] == ["k8s_pod_logs"]
    assert "hunter2hunter2" not in json.dumps(inv.report)
    # what the model saw of the log is wrapped as data
    shown = next(m for who, m in script.calls if who == "workload" and "IGNORE ALL PREVIOUS" in m)
    assert "<untrusted_data" in shown


async def test_no_network_and_no_telemetry_during_a_run(client, crew_on):
    """Only the local test database may be reached. With telemetry left on, crewai 1.15.36 resolves
    telemetry.crewai.com and connects to port 4319; prepare_environment() turns that off."""
    import os

    crew_on.setattr(budget, "complete", happy())
    r, _ = await start(client)
    lookups: list = []
    real = socket.getaddrinfo

    def watch(host, *a, **k):
        if host not in ("localhost", "127.0.0.1", "::1"):
            lookups.append(host)
            raise OSError(f"blocked lookup of {host}")
        return real(host, *a, **k)
    crew_on.setattr(socket, "getaddrinfo", watch)
    await Investigator(uuid.UUID(r.json()["id"]), "t").run()
    inv, _ = await result(r.json()["id"])
    assert inv.status == "completed" and lookups == []
    assert os.environ["CREWAI_DISABLE_TELEMETRY"] == "true" and os.environ["CREWAI_TRACING_ENABLED"] == "false"
    assert inv.report["crewai"]["status"] == "completed"


async def test_the_executor_denies_what_the_person_may_not_read(client, crew_on):
    """A person granted one namespace may not read cluster-scoped Git history. The crew asks anyway; the
    ToolExecutor's own authorization refuses it and the refusal is citable evidence, listed as a gap."""
    class Repos:
        type = "git"
        repositories = ["acme/shop"]
        commits = 0

        def __init__(self, config, credential_ref=None):
            pass

        async def aclose(self):
            pass

        async def recent_commits(self, *a, **k):
            Repos.commits += 1
            return []
    crew_on.setitem(registry.CONNECTOR_TYPES, "git", Repos)
    registry._cache.clear()
    crew_on.setattr(budget, "complete", Script(workload=[final("nothing")], change=[act("recent_commits"), final("denied [E1]")], review=[final(REPORT)]))
    env = f"cw-{uuid.uuid4().hex[:6]}"
    await make_env(env, connectors=[{"type": "kubernetes", "name": "k", "config": {}}, {"type": "git", "name": "g", "config": {}}])
    await make_user(f"{env}@example.com", [f"investigator:{env}:shop"])
    h = await login(client, f"{env}@example.com")
    r = await client.post("/moks/api/investigations/crewai", json={"environment": env, "namespace": "shop", "workload": "payments"}, headers=h)
    assert r.status_code == 201, r.text
    await Investigator(uuid.UUID(r.json()["id"]), "t").run()
    inv, ev = await result(r.json()["id"])
    assert inv.status == "completed" and Repos.commits == 0
    assert ev["E1"].tool == "git_recent_commits" and ev["E1"].kind == "access_denied"
    assert any(g["source"] == "git_recent_commits" for g in inv.report["crewai"]["gaps"])
