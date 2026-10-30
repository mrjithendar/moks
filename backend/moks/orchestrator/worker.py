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
    s = get_settings()
    wid = worker_id()
    stop = stop or asyncio.Event()
    running: set[asyncio.Task] = set()
    log.info("worker started", extra={"event": "worker_started"})
    tick = 0
    while not stop.is_set():
        try:
            if tick % 30 == 0:
                await reap_stale()
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
    # Graceful shutdown: let running investigations finish within the termination grace period.
    if running:
        await asyncio.wait(running, timeout=max(5, s.max_duration_seconds))
