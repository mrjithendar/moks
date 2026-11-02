"""The investigation as a LangGraph state machine.

    triage -> logs -> skills -+-> plan <-> act -+-> correlate -> report
                              |      |          |
                              +----> rules -----+

Without a model, or when one fails, the run takes `rules`, so a report always comes out. The graph
only decides what happens next: each node calls an Investigator method, which keeps policy, budget and
evidence where they were.

After each node the Investigator writes a checkpoint, fenced by the claim's token in the same transaction.
This is deliberately not LangGraph's PostgresSaver: that writes through its own connection, so a worker
that lost its claim could still overwrite a checkpoint, and almost everything worth keeping lives on the
Investigator rather than in graph state. A resumed run enters at the node after the checkpoint, and that
node runs again from the start. All of them are safe to repeat: every tool here is read-only and
counts against the same budget, and findings are restored from before the node began.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..observability import tracer


class State(TypedDict, total=False):
    entry: str
    namespace: str
    round: int
    calls: list[dict]
    use_rules: bool
    finished: bool
    report: Any
    mode: str


def _traced(inv: Any, name: str, fn: Callable[[State], Awaitable[dict]], route: Callable[[State], str]) -> Callable[[State], Awaitable[dict]]:
    async def node(state: State) -> dict:
        with tracer().start_as_current_span(f"node {name}"):
            out = await fn(state)
        merged = {**state, **out}
        await inv._checkpoint(name, route(merged), merged)
        return out
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

    routes: dict[str, Callable[[State], str]] = {
        "triage": lambda s: "logs",
        "logs": lambda s: "skills",
        "skills": lambda s: "plan" if provider else "rules",
        "plan": lambda s: "rules" if s.get("use_rules") else "act" if s.get("calls") else "correlate",
        "act": lambda s: "correlate" if s.get("finished") else "plan",
        "rules": lambda s: "correlate",
        "correlate": lambda s: "report",
        "report": lambda s: END,
    }
    nodes = {"triage": triage, "logs": logs, "skills": skills, "plan": plan, "act": act, "rules": rules, "correlate": correlate, "report": report}
    graph = StateGraph(State)
    for name, fn in nodes.items():
        graph.add_node(name, _traced(inv, name, fn, routes[name]))
    graph.add_conditional_edges(START, lambda s: s.get("entry") or "triage", list(nodes))
    for name, route in routes.items():
        graph.add_conditional_edges(name, route, [*nodes, END])
    return graph.compile()


async def run(inv: Any, executor: Any, ctx: Any, question: str, namespace: str, scope_info: dict, provider: Any, cfg: Any, failures: tuple, resumed: dict | None = None) -> State:
    """`resumed` is the graph state of a checkpoint, with `entry` naming the node to continue at."""
    if resumed and resumed.get("entry") == END:
        return resumed  # the report was written and only publishing it was lost
    compiled = build(inv, executor, ctx, question, scope_info, provider, cfg, failures)
    limit = 2 * inv.settings.max_llm_rounds + 12  # every path is bounded; this only catches a mistake in the graph
    return await compiled.ainvoke(resumed or {"namespace": namespace}, {"recursion_limit": limit})
