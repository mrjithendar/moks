"""Knowledge base: sources (admin), sync, upload, search (access-filtered), index status."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, jobs
from ..auth.deps import get_auth, needs
from ..auth.sessions import AuthContext
from ..config import get_settings
from ..connectors.base import ConnectorError, describe_credential_ref
from ..db import get_db
from ..models import Chunk, Document, IndexOutbox, Job, KnowledgeSource, KnowledgeSyncItem, User
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
    shared_globally: bool = False  # readable by everyone who can read any knowledge: must be chosen, never a default
    team: str | None = Field(default=None, max_length=120)
    classification: Literal["public", "internal", "confidential", "restricted"] = "internal"
    applies_to: dict[str, list[str]] = Field(default_factory=dict)
    authority: int = Field(default=0, ge=-2, le=2)
    review_interval_days: int | None = Field(default=None, ge=1, le=3650)
    stale_policy: Literal["flag", "exclude"] = "flag"
    stale_after_days: int | None = Field(default=None, ge=1, le=3650)

    @model_validator(mode="after")
    def _explicit_sharing(self) -> "SourceIn":
        check_sharing(self.scope_environment, self.scope_namespace, self.shared_globally, self.classification)
        return self


def check_sharing(env: str, ns: str, shared: bool, classification: str) -> None:
    if env == "*" and ns == "*" and not shared:
        raise ValueError("this source would be readable by everyone: name an environment or namespace, or set shared_globally to choose that")
    if shared and classification in ("confidential", "restricted"):
        raise ValueError(f"{classification} content cannot be shared globally")


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
    shared_globally: bool | None = None
    team: str | None = Field(default=None, max_length=120)
    classification: Literal["public", "internal", "confidential", "restricted"] | None = None
    applies_to: dict[str, list[str]] | None = None
    authority: int | None = Field(default=None, ge=-2, le=2)
    review_interval_days: int | None = Field(default=None, ge=1, le=3650)
    stale_policy: Literal["flag", "exclude"] | None = None
    stale_after_days: int | None = Field(default=None, ge=1, le=3650)


SYNC_FIELDS = ("revision", "listing", "content", "state", "started_at", "discovered", "processed", "excluded", "failed", "remaining", "deletions_applied")
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
            "credential_ref": s.credential_ref, "lifecycle": s.lifecycle, "team": s.team, "classification": s.classification, "shared_globally": s.shared_globally,
            "applies_to": s.applies_to or {}, "authority": s.authority, "review_interval_days": s.review_interval_days, "last_reviewed_at": s.last_reviewed_at.isoformat() if s.last_reviewed_at else None,
            "review_overdue": bool(s.review_interval_days and (s.last_reviewed_at is None or datetime.now(timezone.utc) - s.last_reviewed_at > timedelta(days=s.review_interval_days))),
            "stale_policy": s.stale_policy, "stale_after_days": s.stale_after_days, "sync": {k: v for k, v in (s.sync_cursor or {}).items() if k in SYNC_FIELDS}}


@router.get("/search")
async def search(q: str, k: int = 8, environment: str | None = None, namespace: str | None = None, service_name: str | None = None, ctx: AuthContext = Depends(get_auth),
                 db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    if not 2 <= len(q) <= 500:
        raise HTTPException(422, "query must be 2-500 characters")
    try:
        r = await service.search(db, ctx.principal, q, min(max(k, 1), 20), environment=environment, namespace=namespace, service=service_name)
    except ConnectorError as exc:
        raise HTTPException(502, exc.message) from None
    await audit.record(db, action="knowledge.search", actor_id=ctx.user.id, actor_email=ctx.user.email, details={"chars": len(q), "results": len(r["results"])})
    return r


@router.get("/status")
async def status(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    return await service.stats(db)


@router.get("/sources")
async def list_sources(ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict]:
    rows = (await db.execute(select(KnowledgeSource).where(KnowledgeSource.lifecycle != "deleted").order_by(KnowledgeSource.name))).scalars().all()
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
                          owner=body.owner, enabled=body.enabled, sync_interval_minutes=body.sync_interval_minutes if body.type == "github" else 0,
                          shared_globally=body.shared_globally, team=body.team, classification=body.classification, applies_to=body.applies_to, authority=body.authority,
                          review_interval_days=body.review_interval_days, stale_policy=body.stale_policy, stale_after_days=body.stale_after_days)
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
    for key in ("service", "owner", "team", "review_interval_days", "stale_after_days"):  # null clears these
        if key in changes:
            setattr(src, key, changes[key])
    for key in ("scope_environment", "scope_namespace", "enabled", "shared_globally", "classification", "applies_to", "authority", "stale_policy"):
        if changes.get(key) is not None:
            setattr(src, key, changes[key])
    if {"scope_environment", "scope_namespace", "shared_globally", "classification"} & set(changes):  # other edits leave an older source's sharing as it was
        try:
            check_sharing(src.scope_environment, src.scope_namespace, src.shared_globally, src.classification)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
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


class LifecycleIn(BaseModel):
    lifecycle: Literal["active", "quarantined", "archived"]
    reason: str = Field(min_length=3, max_length=500)


@router.put("/sources/{sid}/lifecycle")
async def set_lifecycle(sid: uuid.UUID, body: LifecycleIn, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    """Quarantined and archived sources stop being retrieved at once, because every result is checked against PostgreSQL. Their documents are kept."""
    src = await db.get(KnowledgeSource, sid)
    if src is None or src.lifecycle == "deleted":
        raise HTTPException(404, "Source not found")
    old, src.lifecycle = src.lifecycle, body.lifecycle
    await audit.record(db, action="knowledge.lifecycle", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="knowledge_source", resource_id=str(sid), decision="allow",
                       details={"name": src.name, "from": old, "to": body.lifecycle, "reason": body.reason})
    return source_out(src)


@router.post("/sources/{sid}/review")
async def reviewed(sid: uuid.UUID, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    src = await db.get(KnowledgeSource, sid)
    if src is None or src.lifecycle == "deleted":
        raise HTTPException(404, "Source not found")
    src.last_reviewed_at = datetime.now(timezone.utc)
    await audit.record(db, action="knowledge.review", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="knowledge_source", resource_id=str(sid), decision="allow", details={"name": src.name})
    return source_out(src)


@router.delete("/sources/{sid}")
async def delete_source(sid: uuid.UUID, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    """Soft: the source and its document rows stay as tombstones until the index has dropped them, and for history."""
    src = await db.get(KnowledgeSource, sid)
    if src is None or src.lifecycle == "deleted":
        raise HTTPException(404, "Source not found")
    for doc in (await db.execute(select(Document).where(Document.source_id == sid))).scalars():
        if doc.status != "deleted":
            await service.remove_document(db, doc)  # removes from the search index first (deletion propagation)
    name, src.lifecycle, src.enabled, src.next_sync_at = src.name, "deleted", False, None
    src.name = f"{name[:60]}~{str(src.id)[:8]}"  # the name can be used again
    await audit.record(db, action="knowledge.source_delete", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="knowledge_source",
                       resource_id=str(sid), details={"name": name})
    return {"status": "deleted"}


@router.post("/sources/{sid}/upload", status_code=201)
async def upload(sid: uuid.UUID, file: UploadFile = File(...), namespace: str | None = Form(None), environment: str | None = Form(None),
                 ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    src = await db.get(KnowledgeSource, sid)
    if src is None or src.type != "upload":
        raise HTTPException(404, "Upload source not found")
    # A document can be narrower than its source, never wider: leaving these out means the source's scope.
    scope = {"environment": environment or src.scope_environment, "namespace": namespace or src.scope_namespace}
    if (src.scope_environment != "*" and scope["environment"] != src.scope_environment) or (src.scope_namespace != "*" and scope["namespace"] != src.scope_namespace):
        raise HTTPException(422, "a document cannot be scoped wider than its source")
    name = (file.filename or "document.txt").replace("\\", "/").rsplit("/", 1)[-1][:200]
    data = await file.read(get_settings().rag_max_upload_bytes + 1)
    if len(data) > get_settings().rag_max_upload_bytes:
        raise HTTPException(413, "File too large")
    try:
        doc = await ingest.add_upload(db, src, f"uploads/{name}", data, scope)
    except ConnectorError as exc:
        raise HTTPException(422, exc.message) from None
    await audit.record(db, action="knowledge.upload", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="document",
                       resource_id=str(doc.id), details={"path": doc.path, "status": doc.status, "excluded": doc.excluded_reason})
    return {"id": str(doc.id), "path": doc.path, "status": doc.status, "excluded_reason": doc.excluded_reason}


@router.get("/sources/{sid}/documents")
async def documents(sid: uuid.UUID, status: str | None = None, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict]:
    q = select(Document).where(Document.source_id == sid).order_by(Document.path).limit(2000)
    if status:
        q = q.where(Document.status == status)
    rows = (await db.execute(q)).scalars().all()
    return [{"id": str(d.id), "path": d.path, "title": d.title, "status": d.status, "excluded_reason": d.excluded_reason, "revision": d.revision, "changed_revision": d.changed_revision,
             "service": d.service, "environment": d.environment, "namespace": d.namespace, "url": d.url, "doc_state": d.doc_state, "vector_state": d.vector_state, "vector_error": d.vector_error,
             "behind": d.content_revision != d.indexed_revision, "checked_at": d.checked_at.isoformat() if d.checked_at else None, "updated_at": d.updated_at.isoformat() if d.updated_at else None} for d in rows]


@router.get("/sources/{sid}/failures")
async def failures(sid: uuid.UUID, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    """Files not read, and files excluded. Reasons only: rejected content is never stored, so it cannot be shown."""
    items = (await db.execute(select(KnowledgeSyncItem).where(KnowledgeSyncItem.source_id == sid, KnowledgeSyncItem.state.in_(("failed", "excluded", "pending"))).order_by(KnowledgeSyncItem.state, KnowledgeSyncItem.path).limit(500))).scalars().all()
    ids = (await db.execute(select(Document.id).where(Document.source_id == sid))).scalars().all()
    events = (await db.execute(select(IndexOutbox).where(IndexOutbox.document_id.in_(ids), IndexOutbox.status != "done").order_by(IndexOutbox.id.desc()).limit(200))).scalars().all() if ids else []
    return {"items": [{"path": i.path, "state": i.state, "attempts": i.attempts, "reason": i.last_error, "last_tried_at": i.last_tried_at.isoformat() if i.last_tried_at else None} for i in items],
            "index_events": [{"id": e.id, "document_id": str(e.document_id), "op": e.op, "state": "retrying" if e.status == "pending" and e.attempts else e.status, "attempts": e.attempts, "error": e.error} for e in events]}


@router.post("/sources/{sid}/retry")
async def retry_source(sid: uuid.UUID, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    """Failed files go back in the queue for the current listing, and a pass is queued."""
    n = (await db.execute(update(KnowledgeSyncItem).where(KnowledgeSyncItem.source_id == sid, KnowledgeSyncItem.state == "failed").values(state="pending", last_error=None).returning(KnowledgeSyncItem.path))).all()
    src = await db.get(KnowledgeSource, sid)
    if src is None:
        raise HTTPException(404, "Source not found")
    if n:
        src.sync_cursor = {**(src.sync_cursor or {}), "state": "ingesting", "started_at": datetime.now(timezone.utc).isoformat()}
    await db.commit()
    jid = await jobs.enqueue("knowledge_sync", {"source_id": str(sid)}, dedupe_key=f"knowledge_sync:{sid}", created_by=ctx.user.id)
    await audit.record(db, action="knowledge.retry", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="knowledge_source", resource_id=str(sid), decision="allow", details={"files": len(n)})
    return {"requeued": len(n), "queued": bool(jid)}


@router.get("/documents/{did}")
async def document(did: uuid.UUID, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    """Preview with citations, under the same access rule as search. An excluded document has no text to show."""
    from ..rag import access

    d = await db.get(Document, did)
    if d is None or d.status == "deleted" or not access.can_read(ctx.principal, d.environment, d.namespace):
        raise HTTPException(404, "Document not found")
    s = await db.get(KnowledgeSource, d.source_id)
    chunks = (await db.execute(select(Chunk).where(Chunk.document_id == did).order_by(Chunk.ord).limit(200))).scalars().all()
    return {"id": str(d.id), "title": d.title, "path": d.path, "status": d.status, "excluded_reason": d.excluded_reason, "source": s.name, "revision": d.revision, "changed_revision": d.changed_revision,
            "url": d.url, "doc_state": d.doc_state, "superseded_by": d.superseded_by, "checked_at": d.checked_at.isoformat() if d.checked_at else None, "vector_state": d.vector_state,
            "chunks": [{"ord": c.ord, "heading": c.heading, "text": c.text, "lines": [c.start_line, c.end_line], "offsets": [c.start_offset, c.end_offset], "tokens": c.token_estimate} for c in chunks]}


@router.get("/sources/{sid}/access")
async def access_preview(sid: uuid.UUID, email: str, ctx: AuthContext = Depends(needs(P.ADMIN_USERS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    """Whether a person could retrieve this source, and through which binding. Answers for an administrator, never returns content."""
    from ..actions.service import principal_of
    from ..rag import access

    src, user = await db.get(KnowledgeSource, sid), (await db.execute(select(User).where(User.email == email.strip().lower()))).scalar_one_or_none()
    if src is None or src.lifecycle == "deleted":
        raise HTTPException(404, "Source not found")
    if user is None or not user.is_active:
        return {"email": email, "can_read": False, "reason": "no active account", "grants": []}
    principal = await principal_of(db, user.id)
    readable = access.can_read(principal, src.scope_environment, src.scope_namespace)
    reason = None if src.lifecycle == "active" else f"the source is {src.lifecycle}"
    await audit.record(db, action="knowledge.access_preview", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="knowledge_source", resource_id=str(sid), decision="allow", details={"subject": user.email})
    return {"email": user.email, "can_read": readable and src.lifecycle == "active", "reason": reason or (None if readable else "no binding covers this scope"), "scope": f"{src.scope_environment}/{src.scope_namespace}",
            "shared_globally": src.shared_globally, "grants": [f"{g.environment}/{g.namespace}" for g in access.grants(principal)]}


class RetryIn(BaseModel):
    document_id: uuid.UUID | None = None
    limit: int = Field(default=200, ge=1, le=1000)


@router.post("/outbox/retry")
async def retry_outbox(body: RetryIn | None = None, ctx: AuthContext = Depends(needs(P.ADMIN_LLM)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    """Index changes that ran out of attempts go back in the queue once the index is healthy."""
    body = body or RetryIn()
    n = await service.retry_dead(body.document_id, body.limit)
    await audit.record(db, action="knowledge.outbox_retry", actor_id=ctx.user.id, actor_email=ctx.user.email, decision="allow", details={"requeued": n, "document": str(body.document_id) if body.document_id else None})
    return {"requeued": n}


@router.post("/reindex")
async def reindex(ctx: AuthContext = Depends(needs(P.ADMIN_LLM)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    jid = await jobs.enqueue("knowledge_reindex", {}, dedupe_key="knowledge_reindex", created_by=ctx.user.id)
    return {"queued": bool(jid)}
