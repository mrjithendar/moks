"""Background jobs in PostgreSQL (SKIP LOCKED claiming, dedupe keys, retries with backoff).

Types: knowledge_sync, knowledge_reindex, alert_poll, eval_run. The worker also schedules
periodic jobs (alert polling for auto-investigate environments, GitHub source syncs).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit

from .config import get_settings
from .db import session_scope
from .models import Environment, Job, KnowledgeSource

log = logging.getLogger("moks.jobs")
RATE_LIMIT_MIN_WAIT = 60
LEASE_SECONDS = 120


@dataclass(frozen=True)
class Policy:
    """max_attempts counts failures and crashes alike. retry_abandoned: whether a job whose worker
    died may run again, which is only safe when the work is idempotent or resumable."""
    max_attempts: int
    retry_abandoned: bool


POLICIES: dict[str, Policy] = {
    "knowledge_sync": Policy(3, True),      # incremental: resumes from what it committed
    "knowledge_reindex": Policy(2, True),   # builds a new index version beside the active one
    "alert_poll": Policy(1, False),         # the scheduler queues the next one
    "detection_tick": Policy(1, False),     # the scheduler queues the next one
    "eval_run": Policy(1, False),           # a person starts it again
}
DEFAULT_POLICY = Policy(3, False)
Handler = Callable[[AsyncSession, dict[str, Any]], Awaitable[dict[str, Any]]]


async def enqueue(type_: str, payload: dict[str, Any], *, dedupe_key: str | None = None, created_by=None,
                  delay_seconds: int = 0) -> uuid.UUID | None:
    """Queue a job in its own transaction. Returns None when an identical job is already queued/running."""
    try:
        async with session_scope() as db:
            job = Job(type=type_, payload=payload, dedupe_key=dedupe_key, created_by=created_by,
                      run_after=datetime.now(timezone.utc) + timedelta(seconds=delay_seconds))
            db.add(job)
            await db.flush()
            return job.id
    except IntegrityError:
        return None


async def claim(worker_id: str) -> uuid.UUID | None:
    async with session_scope() as db:
        row = (await db.execute(text("""
            UPDATE jobs SET status = 'running', worker_id = :w, started_at = now(), heartbeat_at = now(), attempts = attempts + 1,
                lease_expires_at = now() + make_interval(secs => :lease)
            WHERE id = (SELECT id FROM jobs WHERE status = 'queued' AND run_after <= now()
                        ORDER BY run_after FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING id"""), {"w": worker_id, "lease": LEASE_SECONDS})).first()
        return row[0] if row else None


async def _heartbeat(job_id: uuid.UUID, worker: str | None, task: asyncio.Task) -> None:
    """Renews the lease. Losing it means another worker may have the job, so this run stops."""
    while True:
        await asyncio.sleep(LEASE_SECONDS / 3)
        async with session_scope() as db:
            renewed = (await db.execute(text("""UPDATE jobs SET heartbeat_at = now(), lease_expires_at = now() + make_interval(secs => :lease)
                                                WHERE id = :id AND status = 'running' AND worker_id = :w RETURNING id"""),
                                        {"lease": LEASE_SECONDS, "id": job_id, "w": worker})).first()
        if renewed is None:
            log.warning("job lease lost", extra={"event": "job_lease_lost"})
            task.cancel()
            return


async def _after_abandon(db: AsyncSession, job: Job) -> None:
    """Leave no state behind that would block the next run."""
    if job.type == "knowledge_reindex":
        await db.execute(text("""UPDATE embedding_indexes SET status = 'failed', error = 'the worker stopped during the build', cleanup_pending = true
                                 WHERE status = 'building'"""))
    if job.type == "eval_run" and job.payload.get("run_id"):
        await db.execute(text("UPDATE eval_runs SET status = 'failed' WHERE id = :id AND status IN ('queued', 'running')"), {"id": uuid.UUID(job.payload["run_id"])})
    if job.type == "knowledge_sync" and job.status == "failed":
        src = await db.get(KnowledgeSource, uuid.UUID(job.payload["source_id"]))
        if src is not None:
            src.last_sync_status, src.last_error = "error", "the worker stopped during the sync"
            await _plan_next(db, src)


async def reap_abandoned() -> list[uuid.UUID]:
    """Running jobs whose worker stopped renewing. Without this their dedupe key would block daily
    sync and detection for that target forever."""
    now = datetime.now(timezone.utc)
    async with session_scope() as db:
        rows = (await db.execute(select(Job).where(Job.status == "running", or_(
            Job.lease_expires_at < now,
            Job.lease_expires_at.is_(None) & (func.coalesce(Job.heartbeat_at, Job.started_at, Job.created_at) < now - timedelta(seconds=LEASE_SECONDS))))
            .with_for_update(skip_locked=True))).scalars().all()
        for job in rows:
            policy = POLICIES.get(job.type, DEFAULT_POLICY)
            retry = policy.retry_abandoned and job.attempts < policy.max_attempts
            job.error, job.worker_id, job.lease_expires_at = "the worker stopped responding", None, None
            if retry:
                job.status, job.run_after = "queued", now + timedelta(seconds=30 * (3 ** max(job.attempts - 1, 0)))
            else:
                job.status, job.finished_at = "failed", now
            await _after_abandon(db, job)
            log.warning("reclaimed abandoned job", extra={"event": "job_reaped", "operation": job.type})
        return [j.id for j in rows]


async def _plan_next(db: AsyncSession, src: KnowledgeSource) -> None:
    src.next_sync_at = datetime.now(timezone.utc) + timedelta(minutes=src.sync_interval_minutes) if src.sync_interval_minutes > 0 else None
    await db.flush()


def handlers() -> dict[str, Handler]:
    from . import alerts, detection
    from .evals import runner
    from .rag import ingest, service

    async def knowledge_sync(db: AsyncSession, p: dict) -> dict:
        src = await db.get(KnowledgeSource, uuid.UUID(p["source_id"]))
        if src is None or not src.enabled:
            return {"skipped": "source missing or disabled"}
        if src.type != "github":
            return {"skipped": f"{src.type} sources are updated on upload/approval"}
        src.last_attempt_at = datetime.now(timezone.utc)
        await db.commit()
        try:
            result = await ingest.sync_github(db, src)
        except Exception as exc:
            status = "rate_limited" if getattr(exc, "kind", None) == "rate_limited" else "error"
            await service.mark_indexed_now(db, src, status, str(getattr(exc, "message", exc))[:500])
            await _plan_next(db, src)
            await audit.record(db, action="knowledge.sync", resource_type="knowledge_source", resource_id=str(src.id), outcome="failed",
                               details={"error": str(getattr(exc, "message", exc))[:300]})
            await db.commit()
            raise
        src = await db.get(KnowledgeSource, src.id)
        src.last_counts = {k: result.get(k, 0) for k in ("added", "updated", "deleted", "unchanged", "excluded", "failed")}
        if not result.get("failed"):
            src.last_success_at = datetime.now(timezone.utc)
        await _plan_next(db, src)
        await audit.record(db, action="knowledge.sync", resource_type="knowledge_source", resource_id=str(src.id), outcome="partial" if result.get("failed") else "success",
                           details=src.last_counts)
        return result

    async def knowledge_reindex(db: AsyncSession, p: dict) -> dict:
        return await service.reindex(db)

    async def alert_poll(db: AsyncSession, p: dict) -> dict:
        return await alerts.poll_environment(uuid.UUID(p["environment_id"]))

    async def eval_run(db: AsyncSession, p: dict) -> dict:
        return await runner.execute(uuid.UUID(p["run_id"]))

    async def detection_tick(db: AsyncSession, p: dict) -> dict:
        return await detection.tick(uuid.UUID(p["environment_id"]))

    return {"detection_tick": detection_tick, "knowledge_sync": knowledge_sync, "knowledge_reindex": knowledge_reindex, "alert_poll": alert_poll, "eval_run": eval_run}


async def run_one(job_id: uuid.UUID) -> None:
    async with session_scope() as db:
        job = await db.get(Job, job_id)
        payload, type_, attempts, worker = dict(job.payload), job.type, job.attempts, job.worker_id
    policy = POLICIES.get(type_, DEFAULT_POLICY)
    fn = handlers().get(type_)
    status, result, error = "succeeded", None, None
    wait: float | None = None
    beat = asyncio.create_task(_heartbeat(job_id, worker, asyncio.current_task()))
    try:
        if fn is None:
            raise ValueError(f"unknown job type {type_}")
        async with session_scope() as db:
            result = await fn(db, payload)
    except asyncio.CancelledError:
        log.warning("job stopped", extra={"event": "job_cancelled", "operation": type_})
        beat.cancel()
        return  # the lease is gone: whoever holds it now owns the outcome
    except Exception as exc:
        log.warning("job failed", extra={"event": "job_failed", "operation": type_, "errorType": type(exc).__name__})
        error = str(getattr(exc, "message", exc))[:1000]
        wait = getattr(exc, "retry_after", None) if type_ != "eval_run" else None
        status = "queued" if wait is not None or attempts < policy.max_attempts else "failed"
    beat.cancel()
    async with session_scope() as db:
        job = await db.get(Job, job_id)
        if job.status != "running" or job.worker_id != worker:
            log.warning("late job result ignored", extra={"event": "job_late_result"})
            return
        job.status, job.result, job.error, job.lease_expires_at = status, result, error, None
        if status == "queued":
            job.run_after = datetime.now(timezone.utc) + timedelta(seconds=max(wait, RATE_LIMIT_MIN_WAIT) if wait is not None else 30 * (3 ** (attempts - 1)))
            if wait is not None:
                job.attempts -= 1  # waiting out a rate limit is not a failed attempt
        else:
            job.finished_at = datetime.now(timezone.utc)


async def schedule_periodic() -> None:
    """Idempotent: dedupe keys keep one pending job per target."""
    s = get_settings()
    async with session_scope() as db:
        envs = (await db.execute(select(Environment.id).where(Environment.auto_investigate.is_(True)))).scalars().all()
        watched = (await db.execute(select(Environment.id).where(Environment.detection_enabled.is_(True)))).scalars().all()
        now = datetime.now(timezone.utc)
        sources = (await db.execute(select(KnowledgeSource).where(KnowledgeSource.enabled.is_(True), KnowledgeSource.type == "github",
                                                                  KnowledgeSource.sync_interval_minutes > 0,
                                                                  or_(KnowledgeSource.next_sync_at.is_(None), KnowledgeSource.next_sync_at <= now)))).scalars().all()
    for env_id in envs:
        await enqueue("alert_poll", {"environment_id": str(env_id)}, dedupe_key=f"alert_poll:{env_id}", delay_seconds=0)
    for env_id in watched:
        await enqueue("detection_tick", {"environment_id": str(env_id)}, dedupe_key=f"detection_tick:{env_id}")
    for src in sources:  # the unique dedupe key is what keeps replicas from double-scheduling
        await enqueue("knowledge_sync", {"source_id": str(src.id)}, dedupe_key=f"knowledge_sync:{src.id}")
    _ = s
    try:
        from .rag import service

        await service.cleanup_retired()
    except Exception:
        log.warning("index cleanup failed", extra={"event": "index_cleanup_failed"})
