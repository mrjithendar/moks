"""MOKS tools as LangChain tools.

The schema is given as plain JSON Schema on purpose: LangChain then passes arguments through untouched,
so the ToolExecutor stays the only validator. With a Pydantic schema here, a bad call would fail inside
LangChain and never reach the audit log or the rejected list the planner learns from.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.tools import StructuredTool

from .contracts import ToolSpec


def as_tools(specs: list[ToolSpec], call: Callable[[str, dict[str, Any], str | None], Awaitable[Any]]) -> dict[str, StructuredTool]:
    """`call` is the policy-checked path. There is no other way for these tools to run."""
    def wrap(spec: ToolSpec) -> StructuredTool:
        async def run(**args: Any) -> Any:
            purpose = args.pop("__purpose", None)
            return await call(spec.name, args, purpose)
        schema = spec.args.model_json_schema()
        schema["title"] = spec.name
        return StructuredTool.from_function(coroutine=run, name=spec.name, description=spec.description or spec.name, args_schema=schema)
    return {s.name: wrap(s) for s in specs}
