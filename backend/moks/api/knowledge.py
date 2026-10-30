"""Knowledge base: sources (admin), sync, upload, search (access-filtered), index status."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, jobs
from ..auth.deps import get_auth, needs
from ..auth.sessions import AuthContext
from ..config import get_settings
from ..connectors.base import ConnectorError, describe_credential_ref
from ..db import get_db
from ..models import Document, Job, KnowledgeSource
from ..rag import ingest, service
from ..rbac import P
from .admin import CredentialRef

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class SourceIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    type: Literal["github", "upload"]
    repository: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$")
    ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_./-]{1,100}$")
    include: list[str] = Field(default_factory=list, max_length=50)
    exclude: list[str] = Field(default_factory=list, max_length=50)
    services: dict[str, str] = Field(default_factory=dict, max_length=100)
    sync_interval_minutes: int = Field(default=1440, ge=0, le=43200)  # daily; 0 turns automatic sync off
    scope_environment: str = Field(default="*", pattern=r"^[a-z0-9*?\-]{1,100}$")
    scope_namespace: str = Field(default="*", pattern=r"^[a-z0-9*?\-]{1,63}$")
    service: str | None = Field(default=None, max_length=100)
    owner: str | None = Field(default=None, max_length=200)
    credential_ref: CredentialRef = Field(default_factory=CredentialRef)
    enabled: bool = True


class SourceUpdate(BaseModel):
    """Name, type and repository cannot change: a different repository is a different source, and
    its documents would all look deleted upstream."""
    model_config = ConfigDict(extra="forbid")
    ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_./-]{1,100}$")
    include: list[str] | None = Field(default=None, max_length=50)
    exclude: list[str] | None = Field(default=None, max_length=50)
    services: dict[str, str] | None = Field(default=None, max_length=100)
    sync_interval_minutes: int | None = Field(default=None, ge=0, le=43200)
    scope_environment: str | None = Field(default=None, pattern=r"^[a-z0-9*?\-]{1,100}$")
    scope_namespace: str | None = Field(default=None, pattern=r"^[a-z0-9*?\-]{1,63}$")
    service: str | None = Field(default=None, max_length=100)
    owner: str | None = Field(default=None, max_length=200)
    credential_ref: CredentialRef | None = None
    enabled: bool | None = None


GITHUB_ONLY = {"ref", "include", "exclude", "services", "sync_interval_minutes", "credential_ref"}


def source_out(s: KnowledgeSource, counts: dict | None = None, job: str | None = None) -> dict[str, Any]:
    stale_after = timedelta(minutes=s.sync_interval_minutes * 2) if s.sync_interval_minutes else None
    return {"sync_interval_minutes": s.sync_interval_minutes, "next_sync_at": s.next_sync_at.isoformat() if s.next_sync_at else None,
            "last_success_at": s.last_success_at.isoformat() if s.last_success_at else None, "last_attempt_at": s.last_attempt_at.isoformat() if s.last_attempt_at else None,
            "last_counts": s.last_counts, "sync_state": job or "idle", "timezone": get_settings().sync_timezone,
            "stale": bool(s.type == "github" and stale_after and (not s.last_success_at or datetime.now(timezone.utc) - s.last_success_at > stale_after)),
            "syncable": s.type == "github", "id": str(s.id), "name": s.name, "type": s.type, "config": s.config, "scope_environment": s.scope_environment,
            "scope_namespace": s.scope_namespace, "service": s.service, "owner": s.owner, "enabled": s.enabled, "revision": s.revision,
            "last_sync_at": s.last_sync_at.isoformat() if s.last_sync_at else None, "last_sync_status": s.last_sync_status,
            "last_error": s.last_error, "documents": counts or {}, "credential": describe_credential_ref(s.credential_ref),
            "credential_ref": s.credential_ref}


@router.get("/search")
async def search(q: str, k: int = 8, environment: str | None = None, ctx: AuthContext = Depends(get_auth),
                 db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    if not 2 <= len(q) <= 500:
        raise HTTPException(422, "query must be 2-500 characters")
    try:
        r = await service.search(db, ctx.principal, q, min(max(k, 1), 20), environment=environment)
    except ConnectorError as exc:
        raise HTTPException(502, exc.message) from None
    await audit.record(db, action="knowledge.search", actor_id=ctx.user.id, actor_email=ctx.user.email, details={"chars": len(q), "results": len(r["results"])})
    return r


@router.get("/status")
async def status(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    return await service.stats(db)


@router.get("/sources")
async def list_sources(ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict]:
    rows = (await db.execute(select(KnowledgeSource).order_by(KnowledgeSource.name))).scalars().all()
    counts: dict[uuid.UUID, dict] = {}
    for sid, st, n in (await db.execute(select(Document.source_id, Document.status, func.count()).group_by(Document.source_id, Document.status))).all():
        counts.setdefault(sid, {})[st] = n
    running = dict((await db.execute(select(Job.dedupe_key, Job.status).where(Job.type == "knowledge_sync", Job.status.in_(["queued", "running"])))).all())
    return [source_out(s, counts.get(s.id), running.get(f"knowledge_sync:{s.id}")) for s in rows]


@router.post("/sources", status_code=201)
async def create_source(body: SourceIn, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    if body.type == "github" and not body.repository:
        raise HTTPException(422, "github sources need a repository")
    cfg = {"repository": body.repository, "ref": body.ref, "include": body.include, "exclude": body.exclude, "services": body.services} if body.type == "github" else {}
    src = KnowledgeSource(name=body.name, type=body.type, config=cfg, credential_ref=body.credential_ref.model_dump(exclude_none=True),
                          scope_environment=body.scope_environment, scope_namespace=body.scope_namespace, service=body.service,
                          owner=body.owner, enabled=body.enabled, sync_interval_minutes=body.sync_interval_minutes if body.type == "github" else 0)
    db.add(src)
    await db.flush()
    await audit.record(db, action="knowledge.source_create", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="knowledge_source",
                       resource_id=str(src.id), details=body.model_dump(mode="json"))
    if body.type == "github":
        await db.commit()
        await jobs.enqueue("knowledge_sync", {"source_id": str(src.id)}, dedupe_key=f"knowledge_sync:{src.id}", created_by=ctx.user.id)
    return source_out(src)


@router.put("/sources/{sid}")
async def update_source(sid: uuid.UUID, body: SourceUpdate, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    """Changes settings and keeps indexed documents. A sync then applies any new include or exclude
    rules; changing a credential alone re-reads nothing."""
    src = await db.get(KnowledgeSource, sid)
    if src is None:
        raise HTTPException(404, "Source not found")
    changes = body.model_dump(exclude_unset=True)
    if src.type != "github" and GITHUB_ONLY & set(changes):
        raise HTTPException(422, f"{src.type} sources have no {', '.join(sorted(GITHUB_ONLY & set(changes)))}")
    old_scope = (src.scope_environment, src.scope_namespace)
    cfg = dict(src.config or {})
    for key in ("ref", "include", "exclude", "services"):
        if key in changes:
            cfg[key] = changes[key]
    src.config = cfg
    for key in ("service", "owner"):  # null clears these
        if key in changes:
            setattr(src, key, changes[key])
    for key in ("scope_environment", "scope_namespace", "enabled"):
        if changes.get(key) is not None:
            setattr(src, key, changes[key])
    if changes.get("credential_ref") is not None:
        src.credential_ref = body.credential_ref.model_dump(exclude_none=True)  # a reference: the value stays in the Secret
    if changes.get("sync_interval_minutes") is not None:
        src.sync_interval_minutes = changes["sync_interval_minutes"]
        src.next_sync_at = datetime.now(timezone.utc) + timedelta(minutes=src.sync_interval_minutes) if src.sync_interval_minutes else None
    moved = 0
    if old_scope != (src.scope_environment, src.scope_namespace):
        try:
            moved = await service.rescope_source(db, src, old_scope)
        except ConnectorError as exc:
            raise HTTPException(502, f"could not update the search index, so nothing was saved: {exc.message}") from None
    await audit.record(db, action="knowledge.source_update", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="knowledge_source",
                       resource_id=str(src.id), decision="allow", details={"name": src.name, "changed": sorted(changes), "credential": describe_credential_ref(src.credential_ref), "documents_rescoped": moved})
    counts = dict((await db.execute(select(Document.status, func.count()).where(Document.source_id == sid).group_by(Document.status))).all())
    return source_out(src, counts)


@router.post("/sources/{sid}/sync")
async def sync(sid: uuid.UUID, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    src = await db.get(KnowledgeSource, sid)
    if src is None or src.type != "github":
        raise HTTPException(404, "GitHub source not found")
    jid = await jobs.enqueue("knowledge_sync", {"source_id": str(sid)}, dedupe_key=f"knowledge_sync:{sid}", created_by=ctx.user.id)
    await audit.record(db, action="knowledge.sync_request", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="knowledge_source", resource_id=str(sid),
                       decision="allow", outcome="queued" if jid else "already_running")
    return {"queued": bool(jid), "already_running": not jid}


class ScheduleIn(BaseModel):
    sync_interval_minutes: int = Field(ge=0, le=43200)  # 0 turns automatic sync off


@router.put("/sources/{sid}/schedule")
async def schedule(sid: uuid.UUID, body: ScheduleIn, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    src = await db.get(KnowledgeSource, sid)
    if src is None or src.type != "github":
        raise HTTPException(404, "GitHub source not found")
    src.sync_interval_minutes = body.sync_interval_minutes
    src.next_sync_at = datetime.now(timezone.utc) + timedelta(minutes=body.sync_interval_minutes) if body.sync_interval_minutes else None
    await audit.record(db, action="knowledge.schedule", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="knowledge_source", resource_id=str(sid),
                       decision="allow", details=body.model_dump())
    return source_out(src)


@router.delete("/sources/{sid}")
async def delete_source(sid: uuid.UUID, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    src = await db.get(KnowledgeSource, sid)
    if src is None:
        raise HTTPException(404, "Source not found")
    for doc in (await db.execute(select(Document).where(Document.source_id == sid))).scalars():
        await service.remove_document(db, doc)  # removes from the search index first (deletion propagation)
    await db.delete(src)
    await audit.record(db, action="knowledge.source_delete", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="knowledge_source",
                       resource_id=str(sid), details={"name": src.name})
    return {"status": "deleted"}


@router.post("/sources/{sid}/upload", status_code=201)
async def upload(sid: uuid.UUID, file: UploadFile = File(...), namespace: str = Form("*"), environment: str = Form("*"),
                 ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    src = await db.get(KnowledgeSource, sid)
    if src is None or src.type != "upload":
        raise HTTPException(404, "Upload source not found")
    name = (file.filename or "document.txt").replace("\\", "/").rsplit("/", 1)[-1][:200]
    data = await file.read(get_settings().rag_max_upload_bytes + 1)
    if len(data) > get_settings().rag_max_upload_bytes:
        raise HTTPException(413, "File too large")
    try:
        doc = await ingest.add_upload(db, src, f"uploads/{name}", data, {"environment": environment, "namespace": namespace})
    except ConnectorError as exc:
        raise HTTPException(422, exc.message) from None
    await audit.record(db, action="knowledge.upload", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="document",
                       resource_id=str(doc.id), details={"path": doc.path, "status": doc.status, "excluded": doc.excluded_reason})
    return {"id": str(doc.id), "path": doc.path, "status": doc.status, "excluded_reason": doc.excluded_reason}


@router.get("/sources/{sid}/documents")
async def documents(sid: uuid.UUID, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict]:
    rows = (await db.execute(select(Document).where(Document.source_id == sid).order_by(Document.path).limit(2000))).scalars().all()
    return [{"id": str(d.id), "path": d.path, "title": d.title, "status": d.status, "excluded_reason": d.excluded_reason, "revision": d.revision,
             "service": d.service, "environment": d.environment, "namespace": d.namespace, "url": d.url,
             "updated_at": d.updated_at.isoformat() if d.updated_at else None} for d in rows]


@router.post("/outbox/retry")
async def retry_outbox(ctx: AuthContext = Depends(needs(P.ADMIN_LLM))) -> dict:
    """Index changes that ran out of attempts go back in the queue once the index is healthy."""
    return {"requeued": await service.retry_dead()}


@router.post("/reindex")
async def reindex(ctx: AuthContext = Depends(needs(P.ADMIN_LLM)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    jid = await jobs.enqueue("knowledge_reindex", {}, dedupe_key="knowledge_reindex", created_by=ctx.user.id)
    return {"queued": bool(jid)}
