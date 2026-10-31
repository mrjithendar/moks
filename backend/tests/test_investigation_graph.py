"""LangGraph drives the investigation; LangChain wraps the model and tools. Neither gets a way around
the budget, the policy executor or the audit log."""
import json
import os
import uuid

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select

from moks.connectors import registry
from moks.db import session_scope
from moks.llm import budget
from moks.llm.lc import MoksChatModel
from moks.llm.providers import LLMError, LLMResponse
from moks.models import AuditEvent, Investigation, LLMConfig
from moks.orchestrator import engine, graph
from moks.orchestrator.engine import Investigator
from moks.tools.contracts import ToolSpec, ToolArgs
from moks.tools.lc import as_tools
from tests.conftest import login, make_env, make_user
from tests.fakes import FakeKubernetes


class Model:
    name = "fake"

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []
        self.cfg = None


@pytest.fixture
def cluster(monkeypatch):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "kubernetes", FakeKubernetes)
    registry._cache.clear()
    yield
    registry._cache.clear()


@pytest.fixture
async def llm(monkeypatch):
    """A model that answers from a script, through budget.complete as any call must."""
    state = {"replies": [], "calls": 0, "system": []}

    async def complete(provider, system, messages, **kw):
        state["calls"] += 1
        state["system"].append((system, kw))
        reply = state["replies"].pop(0) if state["replies"] else '{"tool_calls": [], "done": true}'
        if isinstance(reply, Exception):
            raise reply
        return LLMResponse(reply, 10, 5, "fake", "m")
    monkeypatch.setattr(budget, "complete", complete)
    monkeypatch.setattr(engine, "provider_for", lambda cfg: Model([]))
    async with session_scope() as db:
        for c in (await db.execute(select(LLMConfig).where(LLMConfig.purpose == "chat"))).scalars():
            c.is_active = False
        await db.flush()
        cfg = LLMConfig(name=f"g-{uuid.uuid4().hex[:6]}", purpose="chat", provider="openai", model="m", is_active=True)
        db.add(cfg)
    yield state
    async with session_scope() as db:
        (await db.get(LLMConfig, cfg.id)).is_active = False


async def start(client, question="why is payments crashing"):
    name = f"gr-{uuid.uuid4().hex[:6]}"
    await make_env(name, connectors=[{"type": "kubernetes", "name": "k", "config": {}}])
    await make_user(f"{name}@example.com", [f"investigator:{name}:*"])
    h = await login(client, f"{name}@example.com")
    return uuid.UUID((await client.post("/moks/api/investigations", json={"question": question, "environment": name, "namespace": "shop"}, headers=h)).json()["id"])


async def finished(iid):
    async with session_scope() as db:
        return await db.get(Investigation, iid)


def plan(*calls, done=False):
    return json.dumps({"tool_calls": [{"tool": t, "args": a, "purpose": "test"} for t, a in calls], "done": done})


REPORT = json.dumps({"summary": "done", "observed_facts": [], "suspected_causes": [], "missing_evidence": [], "next_steps": [], "proposed_remediation": []})


def test_the_graph_has_the_documented_shape():
    drawn = graph.build(None, None, None, "q", {}, object(), None, ()).get_graph()
    nodes = set(drawn.nodes) - {"__start__", "__end__"}
    assert nodes == {"triage", "logs", "skills", "plan", "act", "rules", "correlate", "report"}
    edges = {(e.source, e.target) for e in drawn.edges}
    assert {("plan", "act"), ("act", "plan"), ("plan", "rules"), ("act", "correlate"), ("report", "__end__")} <= edges


async def test_without_a_model_the_run_takes_the_rules_path(cluster, client):
    async with session_scope() as db:
        for c in (await db.execute(select(LLMConfig).where(LLMConfig.purpose == "chat"))).scalars():
            c.is_active = False
    iid = await start(client)
    await Investigator(iid, "t").run()
    inv = await finished(iid)
    assert inv.status == "completed" and inv.report["generated_by"] == "rules"


async def test_the_model_plans_tools_run_and_the_loop_ends(cluster, client, llm):
    llm["replies"] = [plan(("k8s_events", {"namespace": "shop"})), plan(done=True), REPORT]
    iid = await start(client)
    await Investigator(iid, "t").run()
    inv = await finished(iid)
    assert inv.status == "completed" and llm["calls"] == 3 and inv.tokens_in == 30
    assert all(kw["investigation_id"] == iid and kw["purpose"] == "investigation" for _, kw in llm["system"])


async def test_rounds_are_bounded_however_much_the_model_asks(cluster, client, llm, monkeypatch):
    llm["replies"] = [plan(("k8s_events", {"namespace": "shop", "limit": i + 1})) for i in range(30)]
    iid = await start(client)
    worker = Investigator(iid, "t")
    await worker.run()
    assert (await finished(iid)).status == "completed"
    assert llm["calls"] <= worker.settings.max_llm_rounds + 2


async def test_a_model_failure_falls_back_to_the_rules_and_still_reports(cluster, client, llm):
    llm["replies"] = [LLMError("unavailable", "provider down"), LLMError("unavailable", "provider down")]
    iid = await start(client)
    await Investigator(iid, "t").run()
    inv = await finished(iid)
    assert inv.status == "completed" and any("LLM planning stopped" in n for n in inv.report["notes"]) and inv.report["generated_by"] == "rules"


async def test_a_tool_the_planner_may_not_use_is_denied_and_audited(cluster, client, llm):
    llm["replies"] = [plan(("restart_deployment", {"name": "x"})), plan(done=True), REPORT]
    iid = await start(client)
    await Investigator(iid, "t").run()
    assert (await finished(iid)).status == "completed"
    async with session_scope() as db:
        denied = (await db.execute(select(AuditEvent).where(AuditEvent.action == "tool.call", AuditEvent.decision == "deny", AuditEvent.resource_id == "restart_deployment"))).scalars().first()
    assert denied is not None


async def test_bad_arguments_reach_the_executor_rather_than_stopping_in_langchain(cluster, client, llm):
    llm["replies"] = [plan(("k8s_events", {"namespace": "NOT VALID!"})), plan(done=True), REPORT]
    iid = await start(client)
    worker = Investigator(iid, "t")
    await worker.run()
    assert (await finished(iid)).status == "completed"
    assert any(r["tool"] == "k8s_events" for r in worker.rejected) or any(e["tool"] == "k8s_events" and not e["ok"] for e in worker.evidence)


async def test_cancelling_stops_the_graph(cluster, client, llm, monkeypatch):
    import asyncio

    llm["replies"] = [plan(("k8s_events", {"namespace": "shop"}))] * 4
    iid = await start(client)
    worker = Investigator(iid, "t")
    real = Investigator._act
    running = asyncio.create_task(worker.run())
    started = asyncio.Event()

    async def act(self, *a):
        started.set()
        await asyncio.sleep(30)  # cancelled here, in a task LangGraph owns, by cancelling the run
        return await real(self, *a)
    monkeypatch.setattr(Investigator, "_act", act)
    await asyncio.wait_for(started.wait(), 10)
    running.cancel()
    await running
    assert (await finished(iid)).status == "cancelled"


async def test_the_chat_model_is_metered_and_keeps_roles(llm):
    llm["replies"] = ["hello"]
    who = uuid.uuid4()
    reply = await MoksChatModel(provider=Model([]), max_tokens=50, user_id=who, json_mode=False, purpose="test").ainvoke([SystemMessage(content="be brief"), HumanMessage(content="hi")])
    assert reply.content == "hello" and reply.usage_metadata["total_tokens"] == 15
    system, kw = llm["system"][0]
    assert system == "be brief" and kw["user_id"] == who and kw["max_tokens"] == 50 and kw["json_mode"] is False


async def test_a_denied_budget_is_not_swallowed(llm):
    llm["replies"] = [budget.BudgetDenied("daily", "over budget")]
    with pytest.raises(budget.BudgetDenied):
        await MoksChatModel(provider=Model([]), max_tokens=50).ainvoke([HumanMessage(content="hi")])


async def test_tools_pass_arguments_untouched_to_the_policy_path():
    class Args(ToolArgs):
        namespace: str

    seen = []

    async def call(tool, args, purpose):
        seen.append((tool, args, purpose))
        return "outcome"

    async def handler(ctx, a):
        raise AssertionError("only the policy path may run a tool")
    tools = as_tools([ToolSpec("t", "a tool", "kubernetes", Args, handler)], call)
    assert await tools["t"].ainvoke({"wrong": 1, "__purpose": "why"}) == "outcome"
    assert await tools["t"].ainvoke({"__purpose": None}) == "outcome"
    assert seen[0] == ("t", {"wrong": 1}, "why") and seen[1][1] == {}


def test_langsmith_cannot_be_switched_on_by_the_environment():
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
        assert os.environ[name] == "false"


def test_no_vendor_chat_package_is_a_dependency():
    from pathlib import Path

    text = (Path(__file__).parents[1] / "requirements.txt").read_text()
    assert "langchain-core" in text and "langgraph" in text and "langchain-openai" not in text and "langchain-anthropic" not in text
