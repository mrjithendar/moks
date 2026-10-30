"""Budgets hold under concurrency, retries, timeouts and crashes."""
import asyncio
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text, update

from moks.config import get_settings
from moks.db import session_scope
from moks.llm import budget
from moks.llm.providers import LLMError, LLMResponse
from moks.models import LLMConfig, LLMReservation, LLMUsage


class Provider:
    """Each call costs what cfg prices say. fail: an LLMError kind, or 'hang' to block."""

    def __init__(self, cfg, fail=None, tokens=(100, 100)):
        self.cfg, self.fail, self.tokens, self.calls = cfg, fail, tokens, 0

    async def complete(self, system, messages, *, json_mode, max_tokens, temperature=0.1):
        self.calls += 1
        await asyncio.sleep(0.01)
        if self.fail == "hang":
            await asyncio.sleep(30)
        if self.fail:
            raise LLMError(self.fail, "failed")
        return LLMResponse(text="{}", tokens_in=self.tokens[0], tokens_out=self.tokens[1], model=self.cfg.model, provider=self.cfg.provider)

    async def embed(self, texts):
        return [[0.0] for _ in texts]


async def _cfg(daily="1", in_price="1000", out_price="1000", concurrency=100):
    async with session_scope() as db:
        cfg = LLMConfig(name=f"b-{uuid.uuid4().hex[:6]}", provider="openai", model="m", purpose="chat", daily_budget_usd=Decimal(daily),
                        input_price_per_mtok=Decimal(in_price), output_price_per_mtok=Decimal(out_price), max_concurrency=concurrency, is_active=False)
        db.add(cfg)
        await db.flush()
        db.expunge(cfg)
        return cfg


async def _call(p, **kw):
    return await budget.complete(p, "s", [{"role": "user", "content": "hello"}], json_mode=True, max_tokens=500, wait_seconds=0, **kw)


async def _held(cfg, status=None):
    async with session_scope() as db:
        q = select(func.count()).select_from(LLMReservation).where(LLMReservation.llm_config_id == cfg.id)
        return (await db.execute(q.where(LLMReservation.status == status) if status else q)).scalar_one()


async def _spent(cfg):
    async with session_scope() as db:
        return (await db.execute(select(func.coalesce(func.sum(LLMUsage.cost_usd), 0)).where(LLMUsage.llm_config_id == cfg.id))).scalar_one()


async def test_concurrent_calls_cannot_overrun_the_last_of_the_budget():
    cfg = await _cfg(daily="1.5")  # each call reserves about 0.503: only two fit
    p = Provider(cfg, tokens=(1, 500))
    results = await asyncio.gather(*(_call(p) for _ in range(12)), return_exceptions=True)
    denied = [r for r in results if isinstance(r, budget.BudgetDenied)]
    # Two run first; settled calls cost 1.0 in all, which then leaves room for no third reservation.
    assert p.calls == 2 and len(denied) == 10
    assert await _spent(cfg) <= Decimal("1.5")


async def test_settling_replaces_the_estimate_with_the_real_cost():
    cfg = await _cfg()
    await _call(Provider(cfg, tokens=(10, 10)))
    async with session_scope() as db:
        row = (await db.execute(select(LLMUsage).where(LLMUsage.llm_config_id == cfg.id))).scalar_one()
    assert row.cost_usd == Decimal("0.02") and row.estimated_usd > row.cost_usd
    assert await _held(cfg, "settled") == 1


async def test_a_definite_refusal_gives_the_capacity_back():
    cfg = await _cfg(daily="0.6")
    with pytest.raises(LLMError):
        await _call(Provider(cfg, fail="rate_limited"))
    assert await _held(cfg, "released") == 1
    await _call(Provider(cfg))


async def test_a_timeout_stays_counted_at_the_full_estimate():
    cfg = await _cfg(daily="0.6")
    with pytest.raises(LLMError):
        await _call(Provider(cfg, fail="timeout"))
    assert await _held(cfg, "uncertain") == 1
    with pytest.raises(budget.BudgetDenied):
        await _call(Provider(cfg))


async def test_cancelling_a_call_is_treated_as_uncertain():
    cfg = await _cfg()
    run = asyncio.create_task(_call(Provider(cfg, fail="hang")))
    await asyncio.sleep(0.05)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert await _held(cfg, "uncertain") == 1


async def test_retries_are_counted_once_each():
    cfg = await _cfg()
    with pytest.raises(LLMError):
        await _call(Provider(cfg, fail="rate_limited"))
    await _call(Provider(cfg, tokens=(100, 100)))
    assert await _spent(cfg) == Decimal("0.2")
    assert await _held(cfg, "settled") == 1 and await _held(cfg) == 2


async def test_a_dead_workers_hold_expires_conservatively():
    cfg = await _cfg(daily="0.6")
    rid = await budget.reserve(cfg, Decimal("0.5"))
    async with session_scope() as db:
        await db.execute(update(LLMReservation).where(LLMReservation.id == rid).values(expires_at=func.now() - timedelta(seconds=1)))
    with pytest.raises(budget.BudgetDenied):
        await _call(Provider(cfg))
    assert await _held(cfg, "uncertain") == 1  # no longer in flight, still counted
    await budget.settle(rid, cfg, Decimal("0.01"), 1, 1)  # the worker came back with the real figure
    await _call(Provider(cfg, tokens=(10, 10)))


async def test_settle_twice_counts_once():
    cfg = await _cfg()
    rid = await budget.reserve(cfg, Decimal("0.1"))
    await budget.settle(rid, cfg, Decimal("0.05"), 1, 1)
    await budget.settle(rid, cfg, Decimal("0.05"), 1, 1)
    assert await _spent(cfg) == Decimal("0.05")


async def test_concurrency_limit_per_provider():
    cfg = await _cfg(concurrency=1)
    first = await budget.reserve(cfg, Decimal("0"))
    with pytest.raises(budget.BudgetDenied) as e:
        await budget.reserve(cfg, Decimal("0"))
    assert e.value.reason == "concurrency"
    await budget.close(first, "released")
    await budget.reserve(cfg, Decimal("0"))


async def test_waiting_gets_a_slot_when_one_frees_up(monkeypatch):
    monkeypatch.setattr(budget, "WAIT_STEP", 0.01)
    cfg = await _cfg(concurrency=1)
    first = await budget.reserve(cfg, Decimal("0"))

    async def later():
        await asyncio.sleep(0.05)
        await budget.close(first, "released")
    asyncio.create_task(later())
    assert await budget.reserve(cfg, Decimal("0"), wait_seconds=2)


async def test_zero_budget_stops_paid_use_but_not_free_models():
    with pytest.raises(budget.BudgetDenied):
        await _call(Provider(await _cfg(daily="0")))
    free = await _cfg(daily="0", in_price="0", out_price="0")
    await _call(Provider(free))


async def test_yesterdays_spend_is_not_today():
    cfg = await _cfg(daily="0.6")
    await _call(Provider(cfg, tokens=(500, 500)))
    async with session_scope() as db:
        await db.execute(text("UPDATE llm_usage SET created_at = date_trunc('day', now(), 'UTC') - interval '1 minute' WHERE llm_config_id = :c"), {"c": cfg.id})
        await db.execute(text("UPDATE llm_reservations SET created_at = date_trunc('day', now(), 'UTC') - interval '1 minute' WHERE llm_config_id = :c"), {"c": cfg.id})
    await _call(Provider(cfg))


async def test_per_user_and_per_investigation_limits(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "llm_user_daily_budget_usd", 0.6)
    monkeypatch.setattr(s, "llm_investigation_budget_usd", 0.6)
    cfg = await _cfg(daily="100")
    user, inv = uuid.uuid4(), uuid.uuid4()
    await _call(Provider(cfg, tokens=(300, 300)), user_id=user)
    with pytest.raises(budget.BudgetDenied) as e:
        await _call(Provider(cfg), user_id=user)
    assert e.value.reason == "user"
    await _call(Provider(cfg, tokens=(300, 300)), investigation_id=inv)
    with pytest.raises(budget.BudgetDenied) as e:
        await _call(Provider(cfg), investigation_id=inv)
    assert e.value.reason == "investigation"


async def test_embeddings_are_metered():
    cfg = await _cfg()
    out = await budget.embed(Provider(cfg), cfg, ["some text to embed"], purpose="indexing")
    assert out == [[0.0]] and await _spent(cfg) > 0
