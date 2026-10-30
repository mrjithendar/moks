"""Knowledge base: sources (admin), sync, upload, search (access-filtered), index status."""
from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, jobs
from ..auth.deps import get_auth, needs
from ..auth.sessions import AuthContext
from ..config import get_settings
from ..connectors.base import ConnectorError
from ..db import get_db
from ..models import Document, KnowledgeSource
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
    sync_interval_minutes: int = Field(default=360, ge=0, le=10080)
    scope_environment: str = Field(default="*", pattern=r"^[a-z0-9*?\-]{1,100}$")
    scope_namespace: str = Field(default="*", pattern=r"^[a-z0-9*?\-]{1,63}$")
    service: str | None = Field(default=None, max_length=100)
    owner: str | None = Field(default=None, max_length=200)
    credential_ref: CredentialRef = Field(default_factory=CredentialRef)
    enabled: bool = True


def source_out(s: KnowledgeSource, counts: dict | None = None) -> dict[str, Any]:
    return {"id": str(s.id), "name": s.name, "type": s.type, "config": s.config, "scope_environment": s.scope_environment,
            "scope_namespace": s.scope_namespace, "service": s.service, "owner": s.owner, "enabled": s.enabled, "revision": s.revision,
            "last_sync_at": s.last_sync_at.isoformat() if s.last_sync_at else None, "last_sync_status": s.last_sync_status,
            "last_error": s.last_error, "documents": counts or {}}


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
    return [source_out(s, counts.get(s.id)) for s in rows]


@router.post("/sources", status_code=201)
async def create_source(body: SourceIn, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    if body.type == "github" and not body.repository:
        raise HTTPException(422, "github sources need a repository")
    cfg = {"repository": body.repository, "ref": body.ref, "include": body.include, "exclude": body.exclude, "services": body.services,
           "sync_interval_minutes": body.sync_interval_minutes} if body.type == "github" else {}
    src = KnowledgeSource(name=body.name, type=body.type, config=cfg, credential_ref=body.credential_ref.model_dump(exclude_none=True),
                          scope_environment=body.scope_environment, scope_namespace=body.scope_namespace, service=body.service,
                          owner=body.owner, enabled=body.enabled)
    db.add(src)
    await db.flush()
    await audit.record(db, action="knowledge.source_create", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="knowledge_source",
                       resource_id=str(src.id), details=body.model_dump(mode="json"))
    if body.type == "github":
        await db.commit()
        await jobs.enqueue("knowledge_sync", {"source_id": str(src.id)}, dedupe_key=f"knowledge_sync:{src.id}", created_by=ctx.user.id)
    return source_out(src)


@router.post("/sources/{sid}/sync")
async def sync(sid: uuid.UUID, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    src = await db.get(KnowledgeSource, sid)
    if src is None or src.type != "github":
        raise HTTPException(404, "GitHub source not found")
    jid = await jobs.enqueue("knowledge_sync", {"source_id": str(sid)}, dedupe_key=f"knowledge_sync:{sid}", created_by=ctx.user.id)
    return {"queued": bool(jid)}


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


@router.post("/reindex")
async def reindex(ctx: AuthContext = Depends(needs(P.ADMIN_LLM)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    jid = await jobs.enqueue("knowledge_reindex", {}, dedupe_key="knowledge_reindex", created_by=ctx.user.id)
    return {"queued": bool(jid)}
