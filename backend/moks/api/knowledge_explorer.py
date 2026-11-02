"""Knowledge Explorer, the retrieval test workspace and feedback. PostgreSQL decides everything here: each
endpoint goes through rag/explore.py's one predicate, and there is no route to OpenSearch itself."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..auth.deps import needs
from ..auth.sessions import AuthContext
from ..connectors.base import ConnectorError
from ..db import get_db
from ..models import Chunk, Document, RetrievalFeedback
from ..rag import access, explore, service
from ..rag import rerank as reranking
from ..rag.chunking import PIPELINE_VERSION
from ..rbac import P
from ..redaction import redact

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
PASSAGE_CHARS = 1500


def _many(value: str) -> set[str]:
    return {v for v in value.split(",") if v}


@router.get("/explorer/documents")
async def browse(q: str = "", source: str = "", environment: str = "", namespace: str = "", service_name: str = Query(default="", alias="service"), resource_type: str = "", kind: str = "",
                 owner: str = "", team: str = "", lifecycle: str = "", classification: str = "", freshness: str = "", indexing: str = "", embedding: str = "",
                 sort: Literal["title", "path", "checked", "chunks"] = "title", descending: bool = False, page: int = 1, size: int = 25,
                 ctx: AuthContext = Depends(needs(P.KNOWLEDGE_READ)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    """Metadata only, with counts and filter options drawn from what this person can see. Search text matches titles, paths and chunk words."""
    filters = {"source": source, "environment": environment, "namespace": namespace, "service": service_name, "resource_type": resource_type, "kind": kind, "owner": owner, "team": team,
               "lifecycle": lifecycle, "classification": classification, "freshness": freshness, "indexing": indexing, "embedding": embedding}
    return await explore.browse(db, ctx.principal, q.strip()[:200] or None, {k: _many(v) for k, v in filters.items()}, sort, descending, max(page, 1), min(max(size, 1), 100))


@router.get("/explorer/documents/{did}")
async def document(did: uuid.UUID, ctx: AuthContext = Depends(needs(P.KNOWLEDGE_READ)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    out = await explore.detail(db, ctx.principal, did)
    if out is None:
        raise HTTPException(404, "Document not found")
    return out


@router.get("/explorer/documents/{did}/chunks")
async def chunks(did: uuid.UUID, offset: int = 0, limit: int = 20, ctx: AuthContext = Depends(needs(P.KNOWLEDGE_READ)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    out = await explore.chunks(db, ctx.principal, did, max(offset, 0), max(limit, 1))
    if out is None:
        raise HTTPException(404, "Document not found")
    return out


@router.post("/explorer/documents/{did}/retry")
async def retry(did: uuid.UUID, ctx: AuthContext = Depends(needs(P.KNOWLEDGE_INDEX)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    """Dead events go back in the queue. A document with none that the index still lacks is queued again."""
    if await explore.get(db, ctx.principal, did) is None:
        raise HTTPException(404, "Document not found")
    await db.commit()
    requeued = await service.retry_dead(did, 20)
    queued = 0 if requeued else await service.requeue(did)
    await audit.record(None, action="knowledge.document_retry", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="document", resource_id=str(did), decision="allow", details={"dead_events": requeued, "queued": queued})
    return {"requeued": requeued + queued}


# ---- retrieval test workspace --------------------------------------------------------------------


class RetrievalTestIn(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    environment: str | None = Field(default=None, max_length=100)
    namespace: str | None = Field(default=None, max_length=253)
    sources: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    service: str | None = Field(default=None, max_length=100)
    resource_type: str | None = Field(default=None, max_length=60)
    k: int = Field(default=8, ge=1, le=20)
    modes: list[Literal["keyword", "vector", "hybrid"]] = Field(default_factory=lambda: ["keyword", "vector", "hybrid"], min_length=1, max_length=3)
    compare_reranking: bool = True
    claim: str | None = Field(default=None, max_length=500)


def _passage(r: dict) -> dict:
    return {**r, "text": redact(r["text"])[:PASSAGE_CHARS], "truncated": len(r["text"]) > PASSAGE_CHARS, "citation": {**r["citation"], "url": service.safe_url(r["citation"].get("url"))}}


def _variant(name: str, rerank: bool, out: dict, diagnostics: bool) -> dict:
    v = {"name": name, "reranked": rerank, "mode": out["mode"], "requested_mode": out.get("requested_mode", name), "fallback_reason": out.get("fallback_reason"), "note": out.get("note"), "abstain": out["abstain"], "evidence": out["evidence"],
         "latency_ms": out.get("latency_ms"), "usage": out.get("usage"), "reranking": out.get("reranking"), "results": [_passage(r) for r in out["results"]], "score_note": out["score_note"]}
    if diagnostics:
        v["diagnostics"] = {"index": out.get("index"), "candidates": out.get("candidates"), "understanding": out.get("understanding")}
    elif v["reranking"]:
        v["reranking"] = {k: v["reranking"].get(k) for k in ("state", "note", "elapsed_ms")}
    return v


@router.post("/retrieval/test")
async def retrieval_test(body: RetrievalTestIn, ctx: AuthContext = Depends(needs(P.KNOWLEDGE_TEST)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    """Bounded: at most four searches and one embedding, each under the caller's own grants. A scope or source the
    person cannot read returns nothing, exactly as search does."""
    q = dict(k=body.k, environment=body.environment, namespace=body.namespace, service=body.service, resource_type=body.resource_type, sources=[str(s) for s in body.sources] or None)
    diagnostics = ctx.principal.can(P.KNOWLEDGE_INDEX)
    prepared = await service.prepare_query(db, ctx.principal, body.query) if any(m != "keyword" for m in body.modes) else None
    usage = {"embedding_calls": int(bool(prepared and prepared[0] is not None)), "tokens": len(body.query) // 4 if prepared and prepared[0] is not None else 0, "tokens_basis": "estimated: embedding providers do not report usage"}
    variants = []
    try:
        for mode in body.modes:
            variants.append(_variant(mode, False, await service.search(db, ctx.principal, body.query, retrieval=mode, rerank=False, prepared=prepared, **q), diagnostics))
        client = reranking.active()
        if body.compare_reranking and client is not None:
            mode = "hybrid" if "hybrid" in body.modes else body.modes[-1]
            variants.append(_variant(f"{mode}+reranker", True, await service.search(db, ctx.principal, body.query, retrieval=mode, rerank=True, prepared=prepared, **q), diagnostics))
    except ConnectorError as exc:
        raise HTTPException(502, exc.message) from None
    await audit.record(db, action="knowledge.retrieval_test", actor_id=ctx.user.id, actor_email=ctx.user.email, details={"chars": len(body.query), "modes": body.modes, "environment": body.environment, "namespace": body.namespace})
    out: dict[str, Any] = {"variants": variants, "usage": usage, "reranker": {"configured": reranking.active() is not None, "note": None if reranking.active() else "Reranking is off, so there is no comparison. Search uses its existing order."},
                           "note": "Documented guidance, not current facts: what is true of the cluster now needs live tool evidence."}
    if body.claim:
        out["claim"] = {"text": body.claim, **service.claim_support(body.claim, variants[-1]["results"])}
    return out


# ---- feedback ------------------------------------------------------------------------------------


class FeedbackIn(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    verdict: Literal["relevant", "irrelevant", "outdated", "missing"]
    chunk_id: uuid.UUID | None = None
    environment: str | None = Field(default=None, max_length=100)
    namespace: str | None = Field(default=None, max_length=253)
    note: str = Field(default="", max_length=1000)
    mode: str | None = Field(default=None, max_length=40)
    reranked: bool | None = None

    @model_validator(mode="after")
    def _about_something(self) -> "FeedbackIn":
        if self.verdict != "missing" and self.chunk_id is None:
            raise ValueError("say which passage this is about")
        return self


@router.post("/feedback", status_code=201)
async def feedback(body: FeedbackIn, ctx: AuthContext = Depends(needs(P.KNOWLEDGE_READ)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    """Review input. Nothing reads this to change ranking, train a model or publish anything."""
    document_id = None
    if body.chunk_id:
        document_id = (await db.execute(select(Chunk.document_id).where(Chunk.id == body.chunk_id))).scalar_one_or_none()
        if document_id is None or await explore.get(db, ctx.principal, document_id) is None:
            raise HTTPException(404, "Passage not found")  # the same answer as for a passage this person cannot read
    elif not access.grants(ctx.principal, body.environment, None if body.namespace in (None, "*") else body.namespace):
        raise HTTPException(404, "Scope not found")
    idx = await service.active_index(db)
    row = RetrievalFeedback(query=redact(body.query), environment=body.environment, namespace=body.namespace, document_id=document_id, chunk_id=body.chunk_id, verdict=body.verdict, note=redact(body.note), created_by=ctx.user.id,
                            context={"mode": body.mode, "reranked": body.reranked, "pipeline": PIPELINE_VERSION, "index": idx.index_name if idx else None, "model": idx.model_key if idx else None})
    db.add(row)
    await db.flush()
    await audit.record(db, action="knowledge.feedback", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="feedback", resource_id=str(row.id), details={"verdict": body.verdict, "document": str(document_id) if document_id else None})
    return {"id": str(row.id), "state": row.state}


async def _reviewable(db: AsyncSession, ctx: AuthContext, row: RetrievalFeedback) -> bool:
    if row.document_id:
        d = await db.get(Document, row.document_id)
        return d is not None and ctx.principal.can(P.KNOWLEDGE_REVIEW, d.environment, d.namespace)
    return ctx.principal.can(P.KNOWLEDGE_REVIEW, row.environment or "*", row.namespace or "*")


@router.get("/feedback")
async def feedback_list(state: str = "open", ctx: AuthContext = Depends(needs(P.KNOWLEDGE_REVIEW)), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict]:
    rows = (await db.execute(select(RetrievalFeedback).where(RetrievalFeedback.state == state).order_by(RetrievalFeedback.created_at.desc()).limit(200))).scalars().all()
    return [{"id": str(r.id), "query": r.query, "verdict": r.verdict, "note": r.note, "environment": r.environment, "namespace": r.namespace, "document_id": str(r.document_id) if r.document_id else None,
             "chunk_id": str(r.chunk_id) if r.chunk_id else None, "context": r.context, "state": r.state, "created_at": r.created_at.isoformat()} for r in rows if await _reviewable(db, ctx, r)]


class FeedbackReviewIn(BaseModel):
    state: Literal["reviewed", "dismissed"]


@router.put("/feedback/{fid}")
async def feedback_review(fid: uuid.UUID, body: FeedbackReviewIn, ctx: AuthContext = Depends(needs(P.KNOWLEDGE_REVIEW)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    row = await db.get(RetrievalFeedback, fid)
    if row is None or not await _reviewable(db, ctx, row):
        raise HTTPException(404, "Feedback not found")
    row.state, row.reviewed_by, row.reviewed_at = body.state, ctx.user.id, datetime.now(timezone.utc)
    await audit.record(db, action="knowledge.feedback_review", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="feedback", resource_id=str(fid), decision="allow", details={"state": body.state})
    return {"id": str(row.id), "state": row.state}
