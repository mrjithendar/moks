"""Investigation worker: claims queued investigations from PostgreSQL and runs them.

Jobs are claimed with SELECT ... FOR UPDATE SKIP LOCKED, so any number of worker replicas can
run without Redis. Runs whose heartbeat stopped (worker crashed) are marked failed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from .. import versions
from ..config import get_settings
from ..db import session_scope
from .engine import Investigator, emit

log = logging.getLogger("moks.worker")

STALE_AFTER = timedelta(minutes=2)


def worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


async def claim(wid: str) -> uuid.UUID | None:
    async with session_scope() as db:
        row = (await db.execute(text(
            """
            UPDATE investigations SET status = 'running', worker_id = :wid, started_at = now(), heartbeat_at = now()
            WHERE id = (
              SELECT id FROM investigations
              WHERE status = 'queued' AND NOT cancel_requested
              ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
            ) RETURNING id
            """
        ), {"wid": wid})).first()
        return row[0] if row else None


async def cancel_queued() -> None:
    """Queued investigations cancelled before any worker picked them up."""
    async with session_scope() as db:
        rows = (await db.execute(text(
            "UPDATE investigations SET status = 'cancelled', finished_at = now(), error = 'cancelled before start' "
            "WHERE status = 'queued' AND cancel_requested RETURNING id"
        ))).all()
    for (iid,) in rows:
        await emit(iid, 10_000, "done", {"status": "cancelled"})


async def reap_stale() -> None:
    cutoff = datetime.now(timezone.utc) - STALE_AFTER
    async with session_scope() as db:
        rows = (await db.execute(text(
            "UPDATE investigations SET status = 'failed', finished_at = now(), error = 'worker stopped responding' "
            "WHERE status = 'running' AND heartbeat_at < :cutoff RETURNING id"
        ), {"cutoff": cutoff})).all()
    for (iid,) in rows:
        log.warning("reaped stale investigation", extra={"event": "investigation_reaped", "investigation_id": str(iid)})
        await emit(iid, 10_001, "done", {"status": "failed"})


async def run_worker(stop: asyncio.Event | None = None) -> None:
    from .. import jobs

    s = get_settings()
    wid = worker_id()
    stop = stop or asyncio.Event()
    jobs.draining.clear()
    running: set[asyncio.Task] = set()
    pools: dict[str, set[asyncio.Task]] = {c: set() for c in jobs.capacity()}
    sizes = jobs.capacity()
    interactive = "interactive" in {c.strip() for c in s.worker_classes.split(",")}
    log.info("worker started", extra={"event": "worker_started"})
    tick = 0
    poll_every = max(1, int(s.alert_poll_seconds / max(s.worker_poll_seconds, 0.1)))
    housekeeping: asyncio.Task | None = None

    from ..skills import registry as skill_registry

    if skill_registry.scan_on_start():
        try:
            await skill_registry.scan()
        except Exception as exc:  # the worker still runs on the last published generation
            log.warning("skill scan failed", extra={"event": "skill_scan_failed", "errorType": type(exc).__name__})

    async def maintain(reap: bool, schedule: bool) -> None:
        await versions.report("worker")
        if reap:
            await reap_stale()
            await jobs.reap_abandoned()
        if schedule:
            await jobs.schedule_periodic()

    async def index_changes() -> None:
        from ..rag import service

        await service.drain_outbox()
        if tick % 60 == 0:
            await service.backfill_vectors()
        if tick % 600 == 0:
            await service.reconcile_index()
            await service.verify_index()

    outbox: asyncio.Task | None = None
    while not stop.is_set():
        try:
            # In its own task: a slow index cleanup must not hold back claiming.
            if (tick % 30 == 0 or tick % poll_every == 0) and (housekeeping is None or housekeeping.done()):
                housekeeping = asyncio.create_task(maintain(tick % 30 == 0, tick % poll_every == 0))
            if "knowledge" in pools and (outbox is None or outbox.done()):  # not a job: a reindex must not hold up deletions
                outbox = asyncio.create_task(index_changes())
            for cls, tasks in pools.items():  # detection first; each class only ever uses its own slots
                while len(tasks) < sizes[cls]:
                    jid = await jobs.claim(wid, cls)
                    if jid is None:
                        break
                    jt = asyncio.create_task(jobs.run_one(jid))
                    tasks.add(jt)
                    jt.add_done_callback(tasks.discard)
            if interactive:
                await cancel_queued()
                while len(running) < s.worker_concurrency:
                    iid = await claim(wid)
                    if iid is None:
                        break
                    log.info("claimed investigation", extra={"event": "investigation_claimed", "investigation_id": str(iid)})
                    t = asyncio.create_task(Investigator(iid, wid).run())
                    running.add(t)
                    t.add_done_callback(running.discard)
        except Exception:
            log.exception("worker loop error", extra={"event": "worker_loop_error"})
        tick += 1
        try:
            await asyncio.wait_for(stop.wait(), timeout=s.worker_poll_seconds)
        except asyncio.TimeoutError:
            pass
    # Graceful shutdown: long background work hands itself back at once, so a rollout never waits
    # for a reindex. Detection and investigations finish within the termination grace period.
    jobs.draining.set()
    for t in (housekeeping, outbox):
        if t is not None:
            t.cancel()
    for cls in ("knowledge", "maintenance"):
        for t in list(pools.get(cls, ())):
            t.cancel()
    pending = running | {t for tasks in pools.values() for t in tasks}
    if pending:
        await asyncio.wait(pending, timeout=max(5, s.max_duration_seconds))
