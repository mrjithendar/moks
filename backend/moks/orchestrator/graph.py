"""The investigation as a LangGraph state machine.

    triage -> logs -> skills -+-> plan <-> act -+-> correlate -> report
                              |      |          |
                              +----> rules -----+

Without a model, or when one fails, the run takes `rules`, so a report always comes out. The graph
only decides what happens next: each node calls an Investigator method, which keeps policy, budget and
evidence where they were. State is not checkpointed: a stopped worker's investigation is taken over by
the existing reaper, and evidence is already in PostgreSQL.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..observability import tracer


class State(TypedDict, total=False):
    namespace: str
    round: int
    calls: list[dict]
    use_rules: bool
    finished: bool
    report: Any
    mode: str


def _traced(name: str, fn: Callable[[State], Awaitable[dict]]) -> Callable[[State], Awaitable[dict]]:
    async def node(state: State) -> dict:
        with tracer().start_as_current_span(f"node {name}"):
            return await fn(state)
    return node


def build(inv: Any, executor: Any, ctx: Any, question: str, scope_info: dict, provider: Any, cfg: Any, failures: tuple) -> Any:
    """`failures` are the errors that end planning, not the run. Everything else, such as revoked
    access, a cancel or the time limit, passes straight through."""
    async def committed() -> None:
        await executor.db.commit()

    async def triage(state: State) -> dict:
        await inv._emit("phase", {"phase": "triage", "label": "Triage: finding what is affected"})
        await inv._emit("phase", {"phase": "evidence", "label": "Collecting baseline evidence"})
        await inv._baseline(executor, ctx, state["namespace"], question)
        namespace = inv.focus_namespace or state["namespace"]
        scope_info["namespace"] = ctx.focus_namespace = namespace
        await committed()
        return {"namespace": namespace}

    async def logs(state: State) -> dict:  # often the only place a failure explains itself
        await inv._collect_logs(executor, ctx, state["namespace"], question)
        await committed()
        return {}

    async def skills(state: State) -> dict:
        await inv._run_skills(executor, ctx, state["namespace"], question)
        await committed()
        return {}

    async def plan(state: State) -> dict:
        number = state.get("round", 0) + 1
        if number > inv.settings.max_llm_rounds or inv.tool_calls >= inv.settings.max_tool_calls:
            return {"calls": [], "finished": True}
        try:
            decided = await inv._plan_round(provider, cfg, inv._with_history(question), scope_info, number, ctx)
        except failures as exc:
            inv.notes.append(f"LLM planning stopped: {getattr(exc, 'message', str(exc))}. Continuing with rule-based follow-ups.")
            await inv._emit("note", {"message": inv.notes[-1]})
            return {"use_rules": True}
        calls = decided.get("tool_calls") if isinstance(decided.get("tool_calls"), list) else []
        return {"round": number, "calls": calls, "finished": not calls or decided.get("done") is True}

    async def act(state: State) -> dict:
        new = await inv._act(executor, ctx, state["calls"])
        await committed()
        return {"finished": state.get("finished", False) or new == 0}

    async def rules(state: State) -> dict:
        await inv._rule_followups(executor, ctx, state["namespace"], question)
        await committed()
        return {}

    async def correlate(state: State) -> dict:
        await inv._emit("phase", {"phase": "correlation", "label": "Correlating changes and symptoms"})
        inv._correlate()
        return {}

    async def report(state: State) -> dict:
        written, mode = await inv._write_report(provider, cfg, question, scope_info)
        return {"report": written, "mode": mode}

    graph = StateGraph(State)
    for name, fn in (("triage", triage), ("logs", logs), ("skills", skills), ("plan", plan), ("act", act), ("rules", rules), ("correlate", correlate), ("report", report)):
        graph.add_node(name, _traced(name, fn))
    graph.add_edge(START, "triage")
    graph.add_edge("triage", "logs")
    graph.add_edge("logs", "skills")
    graph.add_conditional_edges("skills", lambda s: "plan" if provider else "rules", ["plan", "rules"])
    graph.add_conditional_edges("plan", lambda s: "rules" if s.get("use_rules") else "act" if s.get("calls") else "correlate", ["rules", "act", "correlate"])
    graph.add_conditional_edges("act", lambda s: "correlate" if s.get("finished") else "plan", ["plan", "correlate"])
    graph.add_edge("rules", "correlate")
    graph.add_edge("correlate", "report")
    graph.add_edge("report", END)
    return graph.compile()


async def run(inv: Any, executor: Any, ctx: Any, question: str, namespace: str, scope_info: dict, provider: Any, cfg: Any, failures: tuple) -> State:
    compiled = build(inv, executor, ctx, question, scope_info, provider, cfg, failures)
    limit = 2 * inv.settings.max_llm_rounds + 12  # every path is bounded; this only catches a mistake in the graph
    return await compiled.ainvoke({"namespace": namespace}, {"recursion_limit": limit})
