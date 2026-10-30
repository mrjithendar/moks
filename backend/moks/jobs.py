"""Background jobs in PostgreSQL (SKIP LOCKED claiming, dedupe keys, retries with backoff).

Types: knowledge_sync, knowledge_reindex, alert_poll, eval_run. The worker also schedules
periodic jobs (alert polling for auto-investigate environments, GitHub source syncs).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit

from .config import get_settings
from .db import session_scope
from .models import Environment, Job, KnowledgeSource

log = logging.getLogger("moks.jobs")
MAX_ATTEMPTS = 3
RATE_LIMIT_MIN_WAIT = 60
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
            UPDATE jobs SET status = 'running', worker_id = :w, started_at = now(), heartbeat_at = now(), attempts = attempts + 1
            WHERE id = (SELECT id FROM jobs WHERE status = 'queued' AND run_after <= now()
                        ORDER BY run_after FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING id"""), {"w": worker_id})).first()
        return row[0] if row else None


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
        payload, type_, attempts = dict(job.payload), job.type, job.attempts
    fn = handlers().get(type_)
    status, result, error = "succeeded", None, None
    wait: float | None = None
    try:
        if fn is None:
            raise ValueError(f"unknown job type {type_}")
        async with session_scope() as db:
            result = await fn(db, payload)
    except Exception as exc:
        log.warning("job failed", extra={"event": "job_failed", "operation": type_, "errorType": type(exc).__name__})
        error = str(getattr(exc, "message", exc))[:1000]
        wait = getattr(exc, "retry_after", None) if type_ != "eval_run" else None
        status = "queued" if wait is not None or (attempts < MAX_ATTEMPTS and type_ != "eval_run") else "failed"
    async with session_scope() as db:
        job = await db.get(Job, job_id)
        job.status, job.result, job.error = status, result, error
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
