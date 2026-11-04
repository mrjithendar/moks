"""The investigation as a LangGraph state machine.

             +-> knowledge -> triage -> logs -> skills -> refresh -+-> plan <-> act -+-> correlate -> report
             |                                                     |      |          |
    intake --+                                                     +----> rules -----+
             +-> gather -> compose          (deployment guidance, repository questions, execution requests)
             +-> concept                    (conceptual questions: documentation only, nothing live)

`knowledge` searches the index before any live read, and `refresh` once more after triage when the findings
name symptoms the first search did not (orchestrator/knowledge.py); both run whether or not a model plans.
`intake` classifies the request and resolves its target before anything live is read, so a question about
how to deploy something is answered from its sources, not by an incident sweep. Without a model, or when
one fails, the investigation takes `rules`, so a report always comes out. The graph
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

from ..observability import bound, span


class State(TypedDict, total=False):
    entry: str
    namespace: str
    round: int
    calls: list[dict]
    use_rules: bool
    finished: bool
    report: Any
    mode: str
    workflow: str


def _traced(inv: Any, name: str, fn: Callable[[State], Awaitable[dict]], route: Callable[[State], str]) -> Callable[[State], Awaitable[dict]]:
    async def node(state: State) -> dict:
        with bound(node=name), span(f"node {name}", **{"moks.node": name}):
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

    async def intake(state: State) -> dict:
        await inv._emit("phase", {"phase": "intake", "label": "Understanding the request"})
        await inv._intake(executor, ctx, question, state["namespace"], provider, cfg, failures)
        await committed()
        return {"workflow": inv.request.workflow}

    async def gather(state: State) -> dict:
        from . import guidance

        await inv._emit("phase", {"phase": "sources", "label": "Reading the target's sources"})
        await guidance.gather(inv, executor, inv.request)
        await committed()
        return {"namespace": inv.request.namespaces[0] if inv.request.namespaces else state["namespace"]}

    async def compose(state: State) -> dict:
        from . import guidance

        await inv._emit("phase", {"phase": "answer", "label": "Writing the answer"})
        written, mode = await guidance.compose(inv, provider, cfg, inv.request)
        return {"report": written, "mode": mode}

    async def concept(state: State) -> dict:
        from . import guidance

        await inv._emit("phase", {"phase": "answer", "label": "Answering from documentation"})
        written, mode = await guidance.explain(inv, executor, provider, cfg, inv.request)
        await committed()
        return {"report": written, "mode": mode}

    async def knowledge(state: State) -> dict:
        await inv._retrieve(executor, question, "initial", state["namespace"])
        await committed()
        return {}

    async def refresh(state: State) -> dict:
        await inv._retrieve(executor, question, "refresh", state["namespace"])
        await committed()
        return {}

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
        await inv._shared_dependencies(executor, ctx)
        await committed()
        return {}

    async def skills(state: State) -> dict:
        await inv._run_skills(executor, ctx, state["namespace"], question)
        await committed()
        return {}

    async def plan(state: State) -> dict:
        number = state.get("round", 0) + 1
        if number > inv.settings.max_llm_rounds or inv.tool_calls >= inv.tool_budget:
            return {"calls": [], "finished": True}
        try:
            decided = await inv._plan_round(provider, cfg, inv._with_history(question), scope_info, number, ctx)
        except failures as exc:
            inv.notes.append(f"LLM planning stopped: {getattr(exc, 'message', str(exc))}. Continuing with rule-based follow-ups.")
            await inv._emit("note", {"message": inv.notes[-1]})
            return {"use_rules": True}
        inv.consider_expansion(decided.get("expand"))
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
        "intake": lambda s: {"guidance": "gather", "concept": "concept"}.get(s.get("workflow") or "investigate", "knowledge"),
        "knowledge": lambda s: "triage",
        "gather": lambda s: "compose",
        "compose": lambda s: END,
        "concept": lambda s: END,
        "triage": lambda s: "logs",
        "logs": lambda s: "skills",
        "skills": lambda s: "refresh",
        "refresh": lambda s: "plan" if provider else "rules",
        "plan": lambda s: "rules" if s.get("use_rules") else "act" if s.get("calls") else "correlate",
        "act": lambda s: "correlate" if s.get("finished") else "plan",
        "rules": lambda s: "correlate",
        "correlate": lambda s: "report",
        "report": lambda s: END,
    }
    nodes = {"intake": intake, "knowledge": knowledge, "refresh": refresh, "gather": gather, "compose": compose, "concept": concept, "triage": triage, "logs": logs, "skills": skills, "plan": plan, "act": act, "rules": rules, "correlate": correlate, "report": report}
    graph = StateGraph(State)
    for name, fn in nodes.items():
        graph.add_node(name, _traced(inv, name, fn, routes[name]))
    graph.add_conditional_edges(START, lambda s: s.get("entry") or "intake", list(nodes))
    for name, route in routes.items():
        graph.add_conditional_edges(name, route, [*nodes, END])
    return graph.compile()


async def run(inv: Any, executor: Any, ctx: Any, question: str, namespace: str, scope_info: dict, provider: Any, cfg: Any, failures: tuple, resumed: dict | None = None) -> State:
    """`resumed` is the graph state of a checkpoint, with `entry` naming the node to continue at."""
    if resumed and resumed.get("entry") == END:
        return resumed  # the report was written and only publishing it was lost
    compiled = build(inv, executor, ctx, question, scope_info, provider, cfg, failures)
    limit = 2 * inv.settings.max_llm_rounds + 16  # every path is bounded; this only catches a mistake in the graph
    return await compiled.ainvoke(resumed or {"namespace": namespace}, {"recursion_limit": limit})
