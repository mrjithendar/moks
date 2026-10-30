"""A long reindex must not hold back detection, and overload is refused rather than absorbed."""
import asyncio
import time
import uuid

import pytest
from sqlalchemy import select, text

from moks import jobs
from moks.config import get_settings
from moks.db import session_scope
from moks.models import Job
from moks.orchestrator import worker


@pytest.fixture(autouse=True)
async def clean():
    async with session_scope() as db:
        await db.execute(text("DELETE FROM jobs"))
    jobs.draining.clear()
    yield


def _handlers(monkeypatch, **fns):
    real = jobs.handlers()
    monkeypatch.setattr(jobs, "handlers", lambda: {**real, **fns})


async def _status(jid):
    async with session_scope() as db:
        return await db.get(Job, jid)


def test_types_map_to_classes():
    assert jobs.job_class("detection_tick") == "detection" and jobs.job_class("alert_poll") == "detection"
    assert jobs.job_class("knowledge_reindex") == "knowledge" and jobs.job_class("eval_run") == "maintenance"


def test_worker_subscribes_only_to_its_classes(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "worker_classes", "detection")
    monkeypatch.setattr(s, "worker_capacity", "detection=4")
    assert jobs.capacity() == {"detection": 4}


async def test_claim_respects_class():
    jid = await jobs.enqueue("knowledge_reindex", {})
    assert await jobs.claim("w", "detection") is None
    assert await jobs.claim("w", "knowledge") == jid


async def test_blocked_reindex_does_not_delay_detection(monkeypatch):
    monkeypatch.setattr(get_settings(), "worker_poll_seconds", 0.02)
    monkeypatch.setattr(worker, "reap_stale", lambda: asyncio.sleep(0))
    blocked, finished, seen = asyncio.Event(), asyncio.Event(), []

    async def forever(db, p):
        blocked.set()
        await asyncio.Event().wait()

    async def tick(db, p):
        seen.append(time.monotonic())
        finished.set()
        return {}
    _handlers(monkeypatch, knowledge_reindex=forever, detection_tick=tick)
    stop = asyncio.Event()
    run = asyncio.create_task(worker.run_worker(stop))
    try:
        await jobs.enqueue("knowledge_reindex", {})
        await asyncio.wait_for(blocked.wait(), 3)
        queued_at = time.monotonic()
        await jobs.enqueue("detection_tick", {"environment_id": str(uuid.uuid4())})
        await asyncio.wait_for(finished.wait(), 2)
        assert seen[0] - queued_at < 1.5  # the detection target with the reindex stuck
    finally:
        stop.set()
        await asyncio.wait_for(run, 10)


async def test_slow_housekeeping_does_not_stop_claiming(monkeypatch):
    monkeypatch.setattr(get_settings(), "worker_poll_seconds", 0.02)
    monkeypatch.setattr(worker, "reap_stale", lambda: asyncio.sleep(0))
    done = asyncio.Event()

    async def slow():
        await asyncio.sleep(30)

    async def tick(db, p):
        done.set()
        return {}
    monkeypatch.setattr(jobs, "schedule_periodic", slow)
    _handlers(monkeypatch, detection_tick=tick)
    stop = asyncio.Event()
    run = asyncio.create_task(worker.run_worker(stop))
    try:
        await jobs.enqueue("detection_tick", {})
        await asyncio.wait_for(done.wait(), 2)
    finally:
        stop.set()
        await asyncio.wait_for(run, 10)


async def test_one_environment_cannot_take_the_whole_class(monkeypatch):
    monkeypatch.setattr(get_settings(), "job_environment_limit", 1)
    busy, other = str(uuid.uuid4()), str(uuid.uuid4())
    first = await jobs.enqueue("detection_tick", {"environment_id": busy})
    await jobs.enqueue("detection_tick", {"environment_id": busy, "n": 2})
    third = await jobs.enqueue("detection_tick", {"environment_id": other})
    assert await jobs.claim("w", "detection") == first
    assert await jobs.claim("w", "detection") == third  # skips the busy environment's second job
    assert await jobs.claim("w", "detection") is None


async def test_full_queue_refuses_clearly(monkeypatch):
    monkeypatch.setattr(get_settings(), "job_queue_limit", 1)
    await jobs.enqueue("eval_run", {})
    with pytest.raises(jobs.QueueFull):
        await jobs.enqueue("eval_run", {})
    assert await jobs.enqueue("detection_tick", {}) is not None  # other classes are unaffected


async def test_queue_full_is_a_429(client, monkeypatch):
    from tests.conftest import login, make_user

    monkeypatch.setattr(get_settings(), "job_queue_limit", 0)
    who = f"a-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(who, ["admin"])
    h = await login(client, who)
    r = await client.post("/moks/api/knowledge/reindex", headers=h)
    assert r.status_code == 429 and r.headers["Retry-After"] == "30"


async def test_shutdown_hands_a_reindex_back_at_once(monkeypatch):
    monkeypatch.setattr(get_settings(), "worker_poll_seconds", 0.02)
    monkeypatch.setattr(worker, "reap_stale", lambda: asyncio.sleep(0))
    started = asyncio.Event()

    async def forever(db, p):
        started.set()
        await asyncio.Event().wait()
    _handlers(monkeypatch, knowledge_sync=forever)
    jid = await jobs.enqueue("knowledge_sync", {"source_id": str(uuid.uuid4())})
    stop = asyncio.Event()
    run = asyncio.create_task(worker.run_worker(stop))
    await asyncio.wait_for(started.wait(), 3)
    t0 = time.monotonic()
    stop.set()
    await asyncio.wait_for(run, 5)
    job = await _status(jid)
    assert time.monotonic() - t0 < 2 and job.status == "queued" and job.attempts == 0


async def test_lost_lease_cancels_and_late_result_is_ignored(monkeypatch):
    monkeypatch.setattr(jobs, "LEASE_SECONDS", 0.06)
    started = asyncio.Event()

    async def forever(db, p):
        started.set()
        await asyncio.Event().wait()
    _handlers(monkeypatch, eval_run=forever)
    jid = await jobs.enqueue("eval_run", {})
    await jobs.claim("w1", "maintenance")
    run = asyncio.create_task(jobs.run_one(jid))
    await asyncio.wait_for(started.wait(), 2)
    async with session_scope() as db:
        await db.execute(text("UPDATE jobs SET worker_id = 'other' WHERE id = :i"), {"i": jid})
    await asyncio.wait_for(run, 2)
    assert (await _status(jid)).worker_id == "other"


async def test_metrics_report_depth_and_age():
    from moks.observability import JOBS_OLDEST_AGE, JOBS_QUEUED

    await jobs.enqueue("detection_tick", {})
    await asyncio.sleep(0.01)
    await jobs.update_queue_metrics()
    assert JOBS_QUEUED.labels("detection")._value.get() == 1 and JOBS_OLDEST_AGE.labels("detection")._value.get() >= 0
    async with session_scope() as db:
        assert (await db.execute(select(Job.job_class))).scalars().first() == "detection"
