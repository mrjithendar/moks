"""Background jobs in PostgreSQL (SKIP LOCKED claiming, dedupe keys, retries with backoff).

Types: knowledge_sync, knowledge_reindex, alert_poll, detection_tick, eval_run. The worker also
schedules periodic jobs (alert polling for auto-investigate environments, GitHub source syncs).

Every type belongs to a class with its own capacity, so a long reindex cannot delay detection:
detection (alert_poll, detection_tick), knowledge (sync, reindex), maintenance (evaluations and the
rest). Interactive investigations are the fourth class and have their own table. Workers subscribe
to classes, so EKS can scale each one on its own. PostgreSQL remains the only queue.
"""
from __future__ import annotations

import asyncio
import logging
import time
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
from .models import BackupRun, Environment, Job, KnowledgeSource
from .observability import (continue_trace, traceparent, tracer, BACKUP_LAST_RUN_OK, BACKUP_LAST_SUCCESS, DATABASE_SIZE, DATABASE_VOLUME, JOB_DELAY, JOB_RETRIES, JOB_RUNTIME, JOBS_ABANDONED, JOBS_OLDEST_AGE,
                            JOBS_QUEUED, JOBS_REJECTED, JOBS_RUNNING)

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
JOB_CLASSES = {"detection_tick": "detection", "alert_poll": "detection", "knowledge_sync": "knowledge", "knowledge_reindex": "knowledge"}
CLASSES = ("detection", "knowledge", "maintenance")  # the order a worker fills its capacity in


class QueueFull(Exception):
    """Refused rather than queued without limit. Scheduled work tries again on the next tick."""

    def __init__(self, job_class: str):
        super().__init__(f"the {job_class} queue is full; try again shortly")
        self.job_class = job_class


def capacity() -> dict[str, int]:
    s = get_settings()
    wanted = {c.strip() for c in s.worker_classes.split(",") if c.strip()}
    sizes = {k.strip(): int(v) for k, v in (p.split("=") for p in s.worker_capacity.split(",") if "=" in p)}
    return {c: max(1, sizes.get(c, 1)) for c in CLASSES if c in wanted}


def job_class(type_: str) -> str:
    return JOB_CLASSES.get(type_, "maintenance")
Handler = Callable[[AsyncSession, dict[str, Any]], Awaitable[dict[str, Any]]]


async def enqueue(type_: str, payload: dict[str, Any], *, dedupe_key: str | None = None, created_by=None,
                  delay_seconds: int = 0) -> uuid.UUID | None:
    """Queue a job in its own transaction. Returns None when an identical job is already queued/running."""
    cls = job_class(type_)
    try:
        async with session_scope() as db:
            queued = (await db.execute(select(func.count()).select_from(Job).where(Job.job_class == cls, Job.status == "queued"))).scalar_one()
            if queued >= get_settings().job_queue_limit:
                JOBS_REJECTED.labels(cls).inc()
                raise QueueFull(cls)
            parent = traceparent()
            job = Job(type=type_, job_class=cls, payload={**payload, **({"_traceparent": parent} if parent else {})}, dedupe_key=dedupe_key, created_by=created_by,
                      run_after=datetime.now(timezone.utc) + timedelta(seconds=delay_seconds))
            db.add(job)
            await db.flush()
            return job.id
    except IntegrityError:
        return None


async def claim(worker_id: str, cls: str | None = None) -> uuid.UUID | None:
    """The oldest due job. One environment cannot take a whole class: while it is at its limit, its
    jobs are passed over in favour of other environments'."""
    async with session_scope() as db:
        row = (await db.execute(text("""
            UPDATE jobs SET status = 'running', worker_id = :w, started_at = now(), heartbeat_at = now(), attempts = attempts + 1,
                lease_expires_at = now() + make_interval(secs => :lease)
            WHERE id = (SELECT q.id FROM jobs q WHERE q.status = 'queued' AND q.run_after <= now() AND (CAST(:cls AS text) IS NULL OR q.job_class = :cls)
                          AND (q.payload->>'environment_id' IS NULL OR (
                            SELECT count(*) FROM jobs r WHERE r.status = 'running' AND r.job_class = q.job_class
                              AND r.payload->>'environment_id' = q.payload->>'environment_id') < :limit)
                        ORDER BY q.run_after FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING id, job_class, EXTRACT(EPOCH FROM now() - run_after)"""),
            {"w": worker_id, "lease": LEASE_SECONDS, "cls": cls, "limit": get_settings().job_environment_limit})).first()
    if row is None:
        return None
    JOB_DELAY.labels(row[1]).observe(max(float(row[2]), 0))
    return row[0]


async def _heartbeat(job_id: uuid.UUID, worker: str | None, task: asyncio.Task) -> None:
    """Renews the lease. Losing it means another worker may have the job, so this run stops."""
    renewed_at = time.monotonic()
    while True:
        await asyncio.sleep(LEASE_SECONDS / 3)
        started = time.monotonic()
        try:
            async with session_scope() as db:
                renewed = (await db.execute(text("""UPDATE jobs SET heartbeat_at = now(), lease_expires_at = now() + make_interval(secs => :lease)
                                                    WHERE id = :id AND status = 'running' AND worker_id = :w RETURNING id"""),
                                            {"lease": LEASE_SECONDS, "id": job_id, "w": worker})).first()
        except Exception:
            log.warning("job lease renewal failed", extra={"event": "job_lease_renewal_failed"})
            if time.monotonic() - renewed_at < LEASE_SECONDS:
                continue
            renewed = None
        if renewed is None:
            log.warning("job lease lost", extra={"event": "job_lease_lost"})
            task.cancel()
            return
        renewed_at = started


async def _after_abandon(db: AsyncSession, job: Job) -> None:
    """Leave no state behind that would block the next run."""
    if job.type == "knowledge_reindex":
        await db.execute(text("""UPDATE embedding_indexes SET status = 'failed', error = 'the worker stopped during the build', cleanup_pending = true
                                 WHERE status = 'building' AND build_job_id = :job"""), {"job": job.id})
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
    async with session_scope() as db:
        now = (await db.execute(select(func.now()))).scalar_one()
        rows = (await db.execute(select(Job).where(Job.status == "running", or_(
            Job.lease_expires_at < now,
            Job.lease_expires_at.is_(None) & (func.coalesce(Job.heartbeat_at, Job.started_at, Job.created_at) < now - timedelta(seconds=LEASE_SECONDS))))
            .with_for_update(skip_locked=True))).scalars().all()
        for job in rows:
            policy = POLICIES.get(job.type, DEFAULT_POLICY)
            retry = policy.retry_abandoned and job.attempts < policy.max_attempts
            job.error, job.worker_id, job.lease_expires_at = "the worker stopped responding", None, None
            JOBS_ABANDONED.labels(job.type).inc()
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
        if src is None or not src.enabled or src.lifecycle != "active":
            return {"skipped": "source missing, disabled or not active"}
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
        src.last_counts = {k: result.get(k, 0) for k in ("added", "updated", "deleted", "unchanged", "excluded", "failed", "discovered", "processed", "remaining")}
        src.last_counts.update(listing=result.get("listing"), content=result.get("content"))
        if result.get("complete"):
            src.last_success_at = datetime.now(timezone.utc)
        await _plan_next(db, src)
        if result.get("remaining"):
            src.next_sync_at = datetime.now(timezone.utc) + timedelta(seconds=30)  # the same listing carries on, whatever the schedule
        await audit.record(db, action="knowledge.sync", resource_type="knowledge_source", resource_id=str(src.id), outcome="success" if result.get("complete") else "partial",
                           details=src.last_counts)
        return result

    async def knowledge_reindex(db: AsyncSession, p: dict) -> dict:
        return await service.reindex(db, p.get("_job_id"))

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
    with tracer().start_as_current_span(f"job {type_}", context=continue_trace(payload.get("_traceparent"))) as span:
        span.set_attribute("moks.job.id", str(job_id))
        span.set_attribute("moks.job.attempt", attempts)
        await _run_one(job_id, payload, type_, attempts, worker)


async def _run_one(job_id: uuid.UUID, payload: dict, type_: str, attempts: int, worker: str | None) -> None:
    policy = POLICIES.get(type_, DEFAULT_POLICY)
    fn = handlers().get(type_)
    status, result, error = "succeeded", None, None
    started = time.monotonic()
    wait: float | None = None
    beat = asyncio.create_task(_heartbeat(job_id, worker, asyncio.current_task()))
    try:
        if fn is None:
            raise ValueError(f"unknown job type {type_}")
        async with session_scope() as db:
            result = await fn(db, {**payload, "_job_id": job_id})
    except asyncio.CancelledError:
        log.warning("job stopped", extra={"event": "job_cancelled", "operation": type_})
        beat.cancel()
        if draining.is_set():
            await asyncio.shield(_release(job_id, worker))
        return  # otherwise the lease is gone: whoever holds it now owns the outcome
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
        JOB_RUNTIME.labels(job_class(type_), type_).observe(time.monotonic() - started)
        log.info("job finished", extra={"event": "job_finished", "operation": type_, "outcome": "failure" if error else "success", "durationMs": int((time.monotonic() - started) * 1000)})
        if status == "queued":
            JOB_RETRIES.labels(type_).inc()
            job.run_after = datetime.now(timezone.utc) + timedelta(seconds=max(wait, RATE_LIMIT_MIN_WAIT) if wait is not None else 30 * (3 ** (attempts - 1)))
            if wait is not None:
                job.attempts -= 1  # waiting out a rate limit is not a failed attempt
        else:
            job.finished_at = datetime.now(timezone.utc)


draining = asyncio.Event()  # set on shutdown: cancelled work hands itself back at once


async def _release(job_id: uuid.UUID, worker: str | None) -> None:
    """Shutdown, not a crash: a resumable job goes straight back to the queue."""
    async with session_scope() as db:
        job = await db.get(Job, job_id)
        if job.status != "running" or job.worker_id != worker:
            return
        retry = POLICIES.get(job.type, DEFAULT_POLICY).retry_abandoned
        job.worker_id, job.lease_expires_at, job.error = None, None, "the worker shut down"
        job.status = "queued" if retry else "failed"
        if retry:
            job.attempts = max(job.attempts - 1, 0)
        else:
            job.finished_at = datetime.now(timezone.utc)
        await _after_abandon(db, job)


async def update_queue_metrics() -> None:
    async with session_scope() as db:
        rows = (await db.execute(text("""SELECT job_class, count(*) FILTER (WHERE status = 'queued'), count(*) FILTER (WHERE status = 'running'),
                                                COALESCE(EXTRACT(EPOCH FROM now() - min(run_after) FILTER (WHERE status = 'queued' AND run_after <= now())), 0)
                                         FROM jobs WHERE status IN ('queued', 'running') GROUP BY job_class"""))).all()
        investigations = (await db.execute(text("""SELECT count(*) FILTER (WHERE status = 'queued'), count(*) FILTER (WHERE status = 'running'),
                                                   COALESCE(EXTRACT(EPOCH FROM now() - min(created_at) FILTER (WHERE status = 'queued')), 0)
                                                   FROM investigations WHERE status IN ('queued', 'running')"""))).one()
    seen = {r[0]: r for r in rows}
    for cls in CLASSES:
        _, queued, running, age = seen.get(cls, (cls, 0, 0, 0))
        JOBS_QUEUED.labels(cls).set(queued)
        JOBS_RUNNING.labels(cls).set(running)
        JOBS_OLDEST_AGE.labels(cls).set(float(age))
    JOBS_QUEUED.labels("interactive").set(investigations[0])
    JOBS_RUNNING.labels("interactive").set(investigations[1])
    JOBS_OLDEST_AGE.labels("interactive").set(float(investigations[2]))


async def update_durability_metrics() -> None:
    """Age is what the alert uses, so a job that never starts is caught as well as one that fails."""
    s = get_settings()
    async with session_scope() as db:
        DATABASE_SIZE.set((await db.execute(text("SELECT pg_database_size(current_database())"))).scalar_one())
        DATABASE_VOLUME.set(s.database_volume_gib * 1024**3)
        latest = (await db.execute(select(BackupRun).order_by(BackupRun.started_at.desc()).limit(1))).scalars().first()
        good = (await db.execute(select(BackupRun).where(BackupRun.status == "success").order_by(BackupRun.finished_at.desc()).limit(1))).scalars().first()
    if latest is not None:
        BACKUP_LAST_RUN_OK.set(1 if latest.status == "success" else 0)
    if good is not None:
        BACKUP_LAST_SUCCESS.set(good.finished_at.timestamp())


async def schedule_periodic() -> None:
    """Idempotent: dedupe keys keep one pending job per target."""
    s = get_settings()
    try:
        await update_durability_metrics()
        await update_queue_metrics()
    except Exception:
        log.warning("durability metrics failed", extra={"event": "durability_metrics_failed"})
    async with session_scope() as db:
        envs = (await db.execute(select(Environment.id).where(Environment.auto_investigate.is_(True)))).scalars().all()
        watched = (await db.execute(select(Environment.id).where(Environment.detection_enabled.is_(True)))).scalars().all()
        now = datetime.now(timezone.utc)
        sources = (await db.execute(select(KnowledgeSource).where(KnowledgeSource.enabled.is_(True), KnowledgeSource.type == "github",
                                                                  KnowledgeSource.sync_interval_minutes > 0,
                                                                  or_(KnowledgeSource.next_sync_at.is_(None), KnowledgeSource.next_sync_at <= now)))).scalars().all()
    try:
        for env_id in envs:
            await enqueue("alert_poll", {"environment_id": str(env_id)}, dedupe_key=f"alert_poll:{env_id}", delay_seconds=0)
        for env_id in watched:
            await enqueue("detection_tick", {"environment_id": str(env_id)}, dedupe_key=f"detection_tick:{env_id}")
        for src in sources:  # the unique dedupe key is what keeps replicas from double-scheduling
            await enqueue("knowledge_sync", {"source_id": str(src.id)}, dedupe_key=f"knowledge_sync:{src.id}")
    except QueueFull as exc:
        log.warning("queue full, scheduling again next tick", extra={"event": "job_queue_full", "operation": exc.job_class})
    _ = s
    try:
        from .rag import service

        await service.cleanup_retired()
    except Exception:
        log.warning("index cleanup failed", extra={"event": "index_cleanup_failed"})
