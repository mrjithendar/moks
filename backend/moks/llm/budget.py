"""Shared LLM budget: every call reserves its worst-case cost first, inside one lock per provider.

Admission counts settled spend plus open reservations, so concurrent calls cannot all pass a check
and overrun the budget. After the call the reservation becomes the real cost.

Rules that apply to every budget here:
- The day is the UTC calendar day.
- A budget of 0 allows no paid use. Free models (zero price) still run, since they cost nothing.
- No limit is expressed as None (settings only); a provider always has a budget.

When the outcome is unknown (timeout, dropped connection, or a worker that died), the provider may
have charged for it, so the reservation stays counted at its full estimate for that day. Only a
definite refusal gives the capacity back. Each attempt of a retried call has its own reservation
and its own result, so retries are never counted twice.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text, update

from ..config import get_settings
from ..db import session_scope
from ..models import LLMConfig, LLMReservation, LLMUsage
from .providers import LLMError, Message, Provider, complete_metered

log = logging.getLogger("moks.budget")
CHARS_PER_TOKEN = 3            # deliberately pessimistic for English and logs
HOLD_MARGIN_SECONDS = 60
DEFINITE = {"rate_limited", "auth", "config", "bad_request", "invalid_request"}  # refused, so not charged
WAIT_STEP = 0.2


class BudgetDenied(LLMError):
    def __init__(self, reason: str, message: str):
        super().__init__("budget", message)
        self.reason = reason


def estimate(cfg: LLMConfig, chars: int, max_tokens: int) -> Decimal:
    tokens_in = Decimal(chars) / CHARS_PER_TOKEN + 1
    return ((tokens_in * Decimal(cfg.input_price_per_mtok) + Decimal(max_tokens) * Decimal(cfg.output_price_per_mtok)) / Decimal(1_000_000)).quantize(Decimal("0.000001"))


def _today():
    return func.date_trunc("day", func.now(), "UTC")


async def _committed(db, *where) -> Decimal:
    """One statement, so one snapshot: a call settling between two reads would be counted nowhere."""
    used = select(func.coalesce(func.sum(LLMUsage.cost_usd), 0)).where(LLMUsage.created_at >= _today(), *[w[0] for w in where]).scalar_subquery()
    held = select(func.coalesce(func.sum(LLMReservation.estimated_usd), 0)).where(
        LLMReservation.created_at >= _today(), LLMReservation.status.in_(("held", "uncertain")), *[w[1] for w in where]).scalar_subquery()
    return Decimal((await db.execute(select(used + held))).scalar_one())


async def _expire() -> None:
    """A hold that outlived its call belongs to a worker that died: the money may be spent. Its own
    transaction, so a refusal below does not undo it."""
    async with session_scope() as db:
        await db.execute(update(LLMReservation).where(LLMReservation.status == "held", LLMReservation.expires_at < func.now()).values(status="uncertain", settled_at=func.now()))


async def _try_reserve(cfg: LLMConfig, est: Decimal, user_id, investigation_id, purpose: str, seconds: int) -> uuid.UUID:
    s = get_settings()
    await _expire()
    async with session_scope() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"llm:{cfg.id}"})
        if user_id:
            await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"llm-user:{user_id}"})
        inflight = (await db.execute(select(func.count()).select_from(LLMReservation).where(LLMReservation.llm_config_id == cfg.id, LLMReservation.status == "held"))).scalar_one()
        if inflight >= cfg.max_concurrency:
            raise BudgetDenied("concurrency", f"{cfg.name} is at its limit of {cfg.max_concurrency} concurrent calls")
        spent = await _committed(db, (LLMUsage.llm_config_id == cfg.id, LLMReservation.llm_config_id == cfg.id))
        limit = Decimal(cfg.daily_budget_usd)
        if spent + est > limit or (spent > 0 and spent >= limit):  # the second: a free call once the money is gone
            raise BudgetDenied("provider", f"daily LLM budget of ${cfg.daily_budget_usd} for {cfg.name} reached")
        if user_id and s.llm_user_daily_budget_usd is not None and est > 0:
            mine = await _committed(db, (LLMUsage.user_id == user_id, LLMReservation.user_id == user_id))
            if mine + est > Decimal(str(s.llm_user_daily_budget_usd)):
                raise BudgetDenied("user", f"your daily LLM budget of ${s.llm_user_daily_budget_usd} is used")
        if investigation_id and s.llm_investigation_budget_usd is not None and est > 0:
            this = await _committed(db, (LLMUsage.investigation_id == investigation_id, LLMReservation.investigation_id == investigation_id))
            if this + est > Decimal(str(s.llm_investigation_budget_usd)):
                raise BudgetDenied("investigation", f"this investigation's LLM budget of ${s.llm_investigation_budget_usd} is used")
        row = LLMReservation(llm_config_id=cfg.id, user_id=user_id, investigation_id=investigation_id, purpose=purpose, estimated_usd=est,
                             expires_at=datetime.now(timezone.utc) + timedelta(seconds=seconds))
        db.add(row)
        await db.flush()
        return row.id


async def reserve(cfg: LLMConfig, est: Decimal, *, user_id=None, investigation_id=None, purpose: str = "other", wait_seconds: float = 0) -> uuid.UUID:
    """Waits only for a free concurrency slot. A spent budget does not come back by waiting."""
    seconds = cfg.timeout_seconds + HOLD_MARGIN_SECONDS
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while True:
        try:
            return await _try_reserve(cfg, est, user_id, investigation_id, purpose, seconds)
        except BudgetDenied as exc:
            if exc.reason != "concurrency" or asyncio.get_running_loop().time() >= deadline:
                raise
        await asyncio.sleep(WAIT_STEP)


async def settle(reservation_id: uuid.UUID, cfg: LLMConfig, cost: Decimal, tokens_in: int, tokens_out: int) -> None:
    """Idempotent. A late result replaces a conservative hold with the real figure."""
    async with session_scope() as db:
        row = (await db.execute(select(LLMReservation).where(LLMReservation.id == reservation_id).with_for_update())).scalar_one()
        if row.status in ("settled", "released"):
            return
        row.status, row.settled_at = "settled", datetime.now(timezone.utc)
        db.add(LLMUsage(llm_config_id=cfg.id, investigation_id=row.investigation_id, user_id=row.user_id, purpose=row.purpose, provider=cfg.provider, model=cfg.model,
                        tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost, estimated_usd=row.estimated_usd))


async def close(reservation_id: uuid.UUID, status: str) -> None:
    async with session_scope() as db:
        await db.execute(update(LLMReservation).where(LLMReservation.id == reservation_id, LLMReservation.status == "held").values(status=status, settled_at=func.now()))


async def _unknown(reservation_id: uuid.UUID) -> None:
    await close(reservation_id, "uncertain")


async def complete(provider: Provider, system: str, messages: list[Message], *, json_mode: bool, max_tokens: int, user_id=None,
                   investigation_id=None, purpose: str = "other", wait_seconds: float = 10) -> Any:
    """The one way to call a chat model."""
    cfg = provider.cfg
    chars = len(system) + sum(len(str(m.get("content", ""))) for m in messages)
    rid = await reserve(cfg, estimate(cfg, chars, max_tokens), user_id=user_id, investigation_id=investigation_id, purpose=purpose, wait_seconds=wait_seconds)
    try:
        resp = await complete_metered(provider, system, messages, json_mode=json_mode, max_tokens=max_tokens)
    except LLMError as exc:
        await close(rid, "released" if exc.kind in DEFINITE else "uncertain")
        raise
    except BaseException:
        await asyncio.shield(_unknown(rid))  # cancelled mid-call: the provider may have billed it
        raise
    await settle(rid, cfg, resp.cost_usd, resp.tokens_in, resp.tokens_out)
    return resp


async def embed(provider: Provider, cfg: LLMConfig, texts: list[str], *, user_id=None, purpose: str = "embedding", wait_seconds: float = 30) -> list[list[float]]:
    """The one way to call an embedding model. Providers do not report usage, so the estimate is the cost."""
    est = estimate(cfg, sum(len(t) for t in texts), 0)
    rid = await reserve(cfg, est, user_id=user_id, purpose=purpose, wait_seconds=wait_seconds)
    try:
        out = await provider.embed(texts)
    except LLMError as exc:
        await close(rid, "released" if exc.kind in DEFINITE else "uncertain")
        raise
    except BaseException:
        await asyncio.shield(_unknown(rid))
        raise
    await settle(rid, cfg, est, sum(len(t) for t in texts) // CHARS_PER_TOKEN, 0)
    return out
