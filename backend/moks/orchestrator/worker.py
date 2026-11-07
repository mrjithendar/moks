"""Investigation worker: claims queued investigations from PostgreSQL and runs them.

Jobs are claimed with SELECT ... FOR UPDATE SKIP LOCKED, so any number of worker replicas can
run without Redis. Every claim gets its own token and lease. When a lease lapses the reaper takes the
token away, which fences the old worker off, and either requeues the run to resume from its checkpoint
or records why it ended.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from typing import NamedTuple

from sqlalchemy import text

from .. import conversations, crew, versions
from ..config import get_settings
from ..db import session_scope
from ..models import Investigation
from .engine import Investigator, emit, emit_next

log = logging.getLogger("moks.worker")


class Claim(NamedTuple):
    id: uuid.UUID
    token: uuid.UUID
    attempt: int


def crewai_version(classes: set[str]) -> str | None:
    """The crewai version when this worker takes CrewAI investigations: the crewai class, and the image
    built with the crewai extra at the version MOKS was checked against. Otherwise None, and it takes none."""
    if "crewai" not in classes:
        return None
    found = crew.installed_version()
    if found != crew.PINNED:
        log.error("crewai class without crewai %s; taking no CrewAI investigations", crew.PINNED, extra={"event": "crewai_worker_unusable", "outcome": found or "not installed"})
        return None
    return found


def worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


async def claim(wid: str, crewai: bool = False) -> Claim | None:
    """A CrewAI investigation (its queued event carries a "crewai" block) goes only to a worker with the
    crewai class, which runs the moks-crewai image; every other investigation only to the rest."""
    async with session_scope() as db:
        row = (await db.execute(text(
            """
            UPDATE investigations SET status = 'running', worker_id = :wid, started_at = COALESCE(started_at, now()), heartbeat_at = now(),
                   execution_token = gen_random_uuid(), lease_expires_at = now() + make_interval(secs => :lease), attempts = attempts + 1
            WHERE id = (
              SELECT id FROM investigations
              WHERE status = 'queued' AND NOT cancel_requested
                AND EXISTS (SELECT 1 FROM investigation_events e WHERE e.investigation_id = investigations.id AND e.seq = 0
                            AND e.payload->'crewai' IS NOT NULL) = :crewai
              ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
            ) RETURNING id, execution_token, attempts
            """
        ), {"wid": wid, "lease": get_settings().investigation_lease_seconds, "crewai": crewai})).first()
        return Claim(row[0], row[1], row[2]) if row else None


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
    """A lapsed lease means the worker's decision is over: its token goes in the same statement, so
    nothing it does later can land. A run with a checkpoint and attempts left goes back in the queue."""
    s = get_settings()
    async with session_scope() as db:
        rows = (await db.execute(text(
            """
            UPDATE investigations i SET
              status = CASE WHEN cancel_requested THEN 'cancelled'
                            WHEN attempts < :attempts AND EXISTS (SELECT 1 FROM investigation_checkpoints c WHERE c.investigation_id = i.id) THEN 'queued'
                            ELSE 'failed' END,
              finished_at = CASE WHEN NOT cancel_requested AND attempts < :attempts AND EXISTS (SELECT 1 FROM investigation_checkpoints c WHERE c.investigation_id = i.id) THEN NULL ELSE now() END,
              error = CASE WHEN cancel_requested THEN 'cancelled while its worker was not responding'
                           WHEN attempts < :attempts AND EXISTS (SELECT 1 FROM investigation_checkpoints c WHERE c.investigation_id = i.id) THEN NULL
                           ELSE 'worker stopped responding' END,
              execution_token = NULL, lease_expires_at = NULL, worker_id = NULL
            WHERE status = 'running' AND COALESCE(lease_expires_at, heartbeat_at + make_interval(secs => :lease)) < now()
            RETURNING id, status, error
            """
        ), {"attempts": s.investigation_max_attempts, "lease": s.investigation_lease_seconds})).all()
        for iid, status, error in rows:
            if status != "queued":
                inv = await db.get(Investigation, iid)
                await conversations.record_reply(db, inv, error)
    for iid, status, _ in rows:
        log.warning("reaped stale investigation", extra={"event": "investigation_reaped", "investigation_id": str(iid), "outcome": status})
        if status == "queued":
            await emit_next(iid, "note", {"message": "The worker stopped responding. This investigation will resume from its last checkpoint."})
        else:
            await emit(iid, 10_001, "done", {"status": status})


async def run_worker(stop: asyncio.Event | None = None) -> None:
    from .. import jobs

    s = get_settings()
    wid = worker_id()
    stop = stop or asyncio.Event()
    jobs.draining.clear()
    running: set[asyncio.Task] = set()
    pools: dict[str, set[asyncio.Task]] = {c: set() for c in jobs.capacity()}
    sizes = jobs.capacity()
    classes = {c.strip() for c in s.worker_classes.split(",")}
    interactive = "interactive" in classes
    crewai = crewai_version(classes)
    crews: set[asyncio.Task] = set()
    log.info("worker started", extra={"event": "worker_started"})
    tick = 0
    poll_every = max(1, int(s.alert_poll_seconds / max(s.worker_poll_seconds, 0.1)))
    housekeeping: asyncio.Task | None = None

    from ..skills import registry as skill_registry

    if skill_registry.scan_on_start():
        try:
            await skill_registry.register_all()
        except Exception as exc:  # the worker still runs on the last published generation
            log.warning("skill scan failed", extra={"event": "skill_scan_failed", "errorType": type(exc).__name__})

    async def maintain(reap: bool, schedule: bool) -> None:
        await versions.report("worker")
        if crewai:
            await versions.report(crew.WORKER, crewai)
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
            await service.probe_backend()
        if tick % 600 == 0:
            await service.reconcile_index()
            await service.verify_index()
            await service.sync_generation(50)
            await service.cleanup_evaluations()

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
            if interactive or crewai:
                await cancel_queued()
            for kind, tasks, size in ((False, running, s.worker_concurrency if interactive else 0), (True, crews, s.crewai_max_concurrent if crewai else 0)):
                while len(tasks) < size:
                    won = await claim(wid, crewai=kind)
                    if won is None:
                        break
                    log.info("claimed investigation", extra={"event": "investigation_claimed", "investigation_id": str(won.id), "operation": "crewai" if kind else "graph"})
                    t = asyncio.create_task(Investigator(won.id, wid, token=won.token, attempt=won.attempt).run())
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
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
    pending = running | crews | {t for tasks in pools.values() for t in tasks}
    if pending:
        await asyncio.wait(pending, timeout=max(5, s.max_duration_seconds))
