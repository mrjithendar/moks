"""What drafting a query with a model has in common, for PromQL and LogQL alike. The two stay separate
in everything that matters: what they discover, how scope works, and what they check.

A draft is only text for an engineer to review. Nothing here runs it. Its failures read the same in
both places: a clear status, and never a half-understood answer shown as if it were fine.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..llm import budget
from ..llm.providers import LLMError, provider_for
from ..models import LLMConfig
from ..orchestrator.report import parse_json_object

USABLE = "the model did not return a usable answer; try again or write the query yourself"
_running: set[uuid.UUID] = set()


def untrusted(text: str) -> str:
    return "<untrusted_data>" + text.replace("</untrusted_data>", "") + "</untrusted_data>"


def strings(value: Any, limit: int = 10) -> list[str]:
    return [str(v)[:300] for v in value[:limit]] if isinstance(value, list) else []


def whole(value: Any, default: int, low: int, high: int) -> int:
    """A model's number, kept in bounds. JSON booleans are ints in Python, and are not numbers here."""
    return max(low, min(value, high)) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default


@asynccontextmanager
async def one_at_a_time(user_id: uuid.UUID):
    """Per process: the browser prevents repeats, and the budget bounds what gets past this."""
    if user_id in _running:
        raise HTTPException(409, "a query is already being generated")
    _running.add(user_id)  # before the first await, or two requests both pass the check
    try:
        yield
    finally:
        _running.discard(user_id)


async def chat_config(db: AsyncSession) -> LLMConfig:
    cfg = (await db.execute(select(LLMConfig).where(LLMConfig.purpose == "chat", LLMConfig.is_active.is_(True)))).scalar_one_or_none()
    if cfg is None:
        raise HTTPException(409, "no active chat model; configure one under LLM providers")
    return cfg


async def _unless_disconnected(request: Request, coro) -> Any:
    """Stops the model call when the engineer cancels. The budget keeps a cancelled call counted, because it may be billed."""
    task = asyncio.ensure_future(coro)
    while not task.done():
        await asyncio.wait({task}, timeout=0.5)
        if not task.done() and await request.is_disconnected():
            task.cancel()
            raise HTTPException(499, "cancelled")
    return task.result()


async def draft(request: Request, cfg: LLMConfig, system: str, user: str, user_id: uuid.UUID) -> dict:
    """The model's answer as a dictionary whose `query` is a string, or a clear HTTP error."""
    try:
        resp = await _unless_disconnected(request, budget.complete(provider_for(cfg), system, [{"role": "user", "content": user}], json_mode=True,
                                                                   max_tokens=min(cfg.max_output_tokens, 800), user_id=user_id, purpose="query_generation"))
        data = parse_json_object(resp.text)
    except budget.BudgetDenied as exc:
        raise HTTPException(429, exc.message) from None
    except LLMError as exc:
        raise HTTPException(502, f"the model could not answer: {exc.message}") from None
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(502, USABLE) from None
    if not isinstance(data, dict) or not isinstance(data.get("query"), str):
        raise HTTPException(502, USABLE)
    return data
