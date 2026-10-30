"""Execution ownership: a worker that lost its lease stops, writes nothing and replays nothing."""
import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, text, update

from moks.actions import executor as ex
from moks.db import session_scope
from moks.models import ActionPlan
from tests.conftest import login
from tests.fakes import FakeWriter
from tests.test_action_safety import _approve_and_queue, _get, _plan, _setup, fakes  # noqa: F401


async def _queued(client, prod=False):
    t = await _setup(prod=prod)
    h = await login(client, t[1])
    plan = (await _plan(client, h, t[0])).json()
    await _approve_and_queue(client, t, plan)
    return t


async def _claim(client):
    await _queued(client)
    return await ex.claim_plan("owner")


def _use(writer):
    async def factory(env, connector_id=None):
        return writer
    ex.writer_factory = factory


async def _take_over(pid):
    """What the reaper does: the old token stops being valid."""
    async with session_scope() as db:
        await db.execute(update(ActionPlan).where(ActionPlan.id == pid).values(status="reconciliation_required", execution_token=None, lease_expires_at=None))


async def _lapse(pid):
    async with session_scope() as db:
        await db.execute(update(ActionPlan).where(ActionPlan.id == pid).values(lease_expires_at=func.now() - timedelta(seconds=1)))


async def test_each_claim_gets_its_own_token(client):
    await _queued(client)
    await _queued(client)
    first, second = await ex.claim_plan("a"), await ex.claim_plan("b")
    assert first.token != second.token


async def test_renewal_is_refused_after_the_lease_lapses(client):
    claim = await _claim(client)
    await _lapse(claim.plan_id)
    assert await ex.Fence(claim).renew() is False


async def test_renewal_never_shortens_the_lease(client):
    claim = await _claim(client)
    async with session_scope() as db:
        await db.execute(update(ActionPlan).where(ActionPlan.id == claim.plan_id).values(lease_expires_at=func.now() + timedelta(hours=1)))
    before = (await _get(claim.plan_id)).lease_expires_at
    assert await ex.Fence(claim).renew() is True
    assert (await _get(claim.plan_id)).lease_expires_at == before


async def test_a_wrong_token_cannot_renew_or_checkpoint(client):
    claim = await _claim(client)
    wrong = ex.Fence(ex.Claim(claim.plan_id, uuid.uuid4()))
    assert await wrong.renew() is False
    with pytest.raises(ex.LeaseLost):
        await wrong.checkpoint("applied", applied={"forged": True})
    plan = await _get(claim.plan_id)
    assert plan.mutation_state == "none" and plan.checkpoint is None


async def test_stale_checkpoint_after_takeover_changes_nothing(client):
    claim = await _claim(client)
    await _take_over(claim.plan_id)
    with pytest.raises(ex.LeaseLost):
        await ex.Fence(claim).checkpoint("applied")
    plan = await _get(claim.plan_id)
    assert plan.status == "reconciliation_required" and plan.mutation_state == "none"


async def test_a_paused_worker_distrusts_its_lease_without_asking_the_database(client, monkeypatch):
    claim = await _claim(client)
    fence = ex.Fence(claim)
    fence.renewed_at -= ex.LEASE_SECONDS + 1  # the process was frozen
    monkeypatch.setattr(ex, "session_scope", None)  # any database use would crash this test
    with pytest.raises(ex.LeaseLost):
        await fence.verify()


async def test_verify_fails_closed_when_the_database_is_unreachable(client, monkeypatch):
    claim = await _claim(client)
    fence = ex.Fence(claim)

    def down():
        raise ConnectionError("database down")
    monkeypatch.setattr(ex, "session_scope", down)
    with pytest.raises(ex.LeaseLost):
        await fence.verify()
    assert fence.lost


async def test_database_loss_stops_the_work_once_the_lease_could_have_lapsed(client, monkeypatch):
    monkeypatch.setattr(ex, "LEASE_SECONDS", 0.06)
    claim = await _claim(client)
    fence = ex.Fence(claim)

    async def refuse():
        raise ConnectionError("database down")
    monkeypatch.setattr(fence, "renew", refuse)
    work = asyncio.create_task(asyncio.sleep(30))
    await asyncio.wait_for(ex._heartbeat(fence, work), 2)
    with pytest.raises(asyncio.CancelledError):
        await work
    assert fence.lost


async def test_short_database_blip_does_not_stop_the_work(client, monkeypatch):
    monkeypatch.setattr(ex, "LEASE_SECONDS", 0.3)
    claim = await _claim(client)
    fence = ex.Fence(claim)
    real, failed = fence.renew, []

    async def flaky():
        if not failed:
            failed.append(1)
            raise ConnectionError("blip")
        return await real()
    monkeypatch.setattr(fence, "renew", flaky)
    work = asyncio.create_task(asyncio.sleep(30))
    beat = asyncio.create_task(ex._heartbeat(fence, work))
    await asyncio.sleep(0.5)
    assert not work.done() and not fence.lost
    beat.cancel()
    work.cancel()


async def test_no_mutation_when_ownership_is_gone_at_the_intent_checkpoint(client):
    claim = await _claim(client)
    await _take_over(claim.plan_id)
    writer = FakeWriter()
    _use(writer)
    await ex.execute_plan(claim)
    assert writer.calls == []


async def test_lease_lost_during_execution_stops_validation_and_is_not_replayed(client, monkeypatch):
    monkeypatch.setattr(ex, "LEASE_SECONDS", 0.09)
    monkeypatch.setattr(ex, "VALIDATION_TIMEOUT", 30)
    claim = await _claim(client)
    writer = FakeWriter("stuck")  # validation would otherwise wait 30 seconds
    _use(writer)
    run = asyncio.create_task(ex.execute_plan(claim))
    while not writer.calls:
        await asyncio.sleep(0.005)
    await _lapse(claim.plan_id)
    await asyncio.wait_for(run, 2)
    assert (await _get(claim.plan_id)).status == "executing"  # the stale worker reported nothing
    assert await ex.reconcile_stale() == [claim.plan_id]
    plan = await _get(claim.plan_id)
    assert plan.status == "reconciliation_required" and plan.execution_token is None
    assert await ex.claim_plan("next") is None
    assert len(writer.calls) == 1


async def test_late_result_cannot_overwrite_the_reconciled_outcome(client):
    claim = await _claim(client)
    await ex.Fence(claim).checkpoint("intent")
    writer = FakeWriter()
    _use(writer)

    async def fast_forward(fence, snapshot, identity, env, timeout, result):
        await _take_over(claim.plan_id)  # the worker paused, and the reaper ran meanwhile
        return "succeeded"
    real, ex._perform = ex._perform, fast_forward
    try:
        await ex.execute_plan(claim)
    finally:
        ex._perform = real
    plan = await _get(claim.plan_id)
    assert plan.status == "reconciliation_required" and plan.result["late_result"]["status"] == "succeeded"


async def test_lease_lost_before_recovery_touches_nothing(client):
    claim = await _claim(client)
    writer = FakeWriter("crashloop")
    _use(writer)
    real = ex._recover

    async def lose_then_recover(fence, *args):
        await _take_over(claim.plan_id)
        return await real(fence, *args)
    ex._recover = lose_then_recover
    try:
        await ex.execute_plan(claim)
    finally:
        ex._recover = real
    assert writer.restored is False and len(writer.calls) == 1


async def test_recovery_intent_is_durable_before_the_patch(client):
    claim = await _claim(client)
    writer = FakeWriter("crashloop")
    _use(writer)
    await ex.execute_plan(claim)
    plan = await _get(claim.plan_id)
    assert "recovery_intent" in plan.checkpoint


async def test_reaper_uses_database_time_and_clears_ownership(client):
    claim = await _claim(client)
    await ex.Fence(claim).checkpoint("intent")
    await _lapse(claim.plan_id)
    assert await ex.reconcile_stale() == [claim.plan_id]
    plan = await _get(claim.plan_id)
    assert plan.status == "reconciliation_required"
    async with session_scope() as db:
        assert (await db.execute(text("SELECT execution_token FROM action_plans WHERE id = :i"), {"i": claim.plan_id})).scalar() is None
