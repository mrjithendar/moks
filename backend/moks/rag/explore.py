"""What the Knowledge Explorer shows: documents, their chunks and how far each got through sync, indexing and
embedding. Everything here reads PostgreSQL, which holds the text, and OpenSearch is never asked, so this cannot
become a way around document access.

One predicate decides what a person sees, and the list, the filter options, every count and each direct link
all use it. So a document someone cannot read does not appear in a total, does not offer a filter value, and
answers an id exactly as a document that does not exist.

  reader    active documents of active sources, with one binding covering both axes (access.py)
  reviewer  also drafts, quarantined, archived and excluded ones, in the scope of a knowledge.review binding
  index     knowledge.index adds retry and error detail to what the person can already see, and no documents
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Chunk, Document, DocumentIndexState, EmbeddingIndex, IndexOutbox, KnowledgeSource
from ..rbac import P, Principal
from ..redaction import redact
from . import access, service
from .chunking import PIPELINE_VERSION

MAX_DOCUMENTS = 20_000  # beyond this the page says its counts are partial
PREVIEW_CHARS = 40_000
PAGE_CHUNKS = 50


def visible(principal: Principal, *only: str) -> tuple[Any, dict]:
    """The one predicate. Whole bindings, so environment and namespace stay together."""
    params: dict[str, Any] = {"realm": access.realm()}
    read = access.sql(access.grants(principal), params, "documents", "r")
    review = access.sql(access.grants(principal, perm=P.KNOWLEDGE_REVIEW), params, "documents", "v")
    clause = f"""documents.realm = :realm AND knowledge_sources.realm = :realm AND documents.status <> 'deleted' AND knowledge_sources.lifecycle <> 'deleted'
        AND (({read} AND documents.status = 'active' AND documents.lifecycle = 'active' AND knowledge_sources.lifecycle = 'active') OR {review})"""
    for extra in only:
        clause += f" AND {extra}"
    return text(clause), params


async def _rows(db: AsyncSession, principal: Principal, query: str | None = None, document_id: uuid.UUID | None = None, limit: int = MAX_DOCUMENTS + 1) -> list[tuple[Document, KnowledgeSource]]:
    extras = []
    if query:
        extras.append("(documents.title ILIKE :like OR documents.path ILIKE :like OR EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = documents.id AND c.tsv @@ websearch_to_tsquery('english', :q)))")
    clause, params = visible(principal, *extras)
    if query:
        params |= {"q": query, "like": "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"}
    stmt = select(Document, KnowledgeSource).join(KnowledgeSource, KnowledgeSource.id == Document.source_id).where(clause.bindparams(**params))
    if document_id:
        stmt = stmt.where(Document.id == document_id)
    return [(d, s) for d, s in (await db.execute(stmt.order_by(Document.path, Document.id).limit(limit))).all()]


async def get(db: AsyncSession, principal: Principal, document_id: uuid.UUID) -> tuple[Document, KnowledgeSource] | None:
    """None for a document that does not exist and for one this person may not see, alike."""
    rows = await _rows(db, principal, document_id=document_id, limit=1)
    return rows[0] if rows else None


def _indexing(d: Document, chunks: int, state: DocumentIndexState | None, idx: EmbeddingIndex | None, dead: bool) -> str:
    if d.status != "active":
        return "not_indexed"
    if dead:
        return "failed"
    if d.content_revision != d.indexed_revision:
        return "pending"
    if idx is None or state is None or state.revision != d.content_revision or state.chunks != chunks:
        return "incomplete"
    return "indexed"


def _embedding(d: Document, chunks: int, state: DocumentIndexState | None, idx: EmbeddingIndex | None) -> dict:
    """From what this generation recorded, never from the fact that a generation is active."""
    if d.status != "active" or not chunks:
        return {"state": "not_applicable", "vectors": 0, "expected": 0, "coverage": None}
    if idx is None or not idx.dimensions:
        return {"state": "keyword_only", "vectors": 0, "expected": 0, "coverage": None}
    vectors = state.vectors if state is not None and state.revision == d.content_revision else 0
    return {"state": "complete" if vectors >= chunks else "partial" if vectors else "missing", "vectors": vectors, "expected": chunks, "coverage": round(min(vectors / chunks, 1), 4)}


def _when(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _summary(d: Document, s: KnowledgeSource, chunks: int, state: DocumentIndexState | None, idx: EmbeddingIndex | None, dead: bool, now: datetime) -> dict:
    freshness, why = service._freshness(s, d, now)
    return {"id": str(d.id), "title": d.title or d.path.rsplit("/", 1)[-1], "path": d.path, "kind": d.kind, "status": d.status, "excluded_reason": d.excluded_reason,
            "source": {"id": str(s.id), "name": s.name, "type": s.type, "lifecycle": s.lifecycle, "last_success_at": _when(s.last_success_at)},
            "environment": d.environment, "namespace": d.namespace, "service": d.service or "", "resource_types": sorted(str(x) for x in (s.applies_to or {}).get("resources") or []),
            "owner": d.owner or s.owner or "", "team": s.team or "", "classification": s.classification, "lifecycle": d.lifecycle, "trust": d.trust,
            "revision": d.revision, "checked_at": _when(d.checked_at), "updated_at": _when(d.updated_at), "chunks": chunks,
            "freshness": {"state": freshness, "reason": why}, "indexing": _indexing(d, chunks, state, idx, dead), "embedding": _embedding(d, chunks, state, idx), "note": d.ingest_note}


async def _index_facts(db: AsyncSession, ids: list[uuid.UUID]) -> tuple[EmbeddingIndex | None, dict, dict, set]:
    idx = await service.active_index(db)
    counts = dict((await db.execute(select(Chunk.document_id, func.count()).where(Chunk.document_id.in_(ids)).group_by(Chunk.document_id))).all()) if ids else {}
    states = {s.document_id: s for s in (await db.execute(select(DocumentIndexState).where(DocumentIndexState.index_id == idx.id, DocumentIndexState.document_id.in_(ids)))).scalars()} if idx and ids else {}
    dead = set((await db.execute(select(IndexOutbox.document_id).where(IndexOutbox.status == "dead", IndexOutbox.document_id.in_(ids)).distinct())).scalars()) if ids else set()
    return idx, counts, states, dead


# Filter name to the value a document has. Applied here rather than in SQL, so every filter and count works on
# exactly the rows the predicate returned.
FACETS: dict[str, Any] = {
    "source": lambda r: r["source"]["name"], "environment": lambda r: r["environment"], "namespace": lambda r: r["namespace"], "service": lambda r: r["service"],
    "resource_type": lambda r: r["resource_types"], "kind": lambda r: r["kind"], "owner": lambda r: r["owner"], "team": lambda r: r["team"], "lifecycle": lambda r: r["lifecycle"],
    "classification": lambda r: r["classification"], "freshness": lambda r: r["freshness"]["state"], "indexing": lambda r: r["indexing"], "embedding": lambda r: r["embedding"]["state"],
}
SORTS = {"title": lambda r: r["title"].lower(), "path": lambda r: r["path"], "checked": lambda r: r["checked_at"] or "", "chunks": lambda r: r["chunks"]}


def _values(row: dict, facet: str) -> list[str]:
    value = FACETS[facet](row)
    return [str(v) for v in (value if isinstance(value, list) else [value]) if v not in (None, "")]


def _matches(row: dict, filters: dict[str, set[str]], skip: str | None = None) -> bool:
    return all(not wanted or wanted & set(_values(row, facet)) for facet, wanted in filters.items() if facet != skip)


async def browse(db: AsyncSession, principal: Principal, query: str | None, filters: dict[str, set[str]], sort: str = "title", descending: bool = False, page: int = 1, size: int = 25) -> dict:
    found = await _rows(db, principal, query)
    capped = len(found) > MAX_DOCUMENTS
    found = found[:MAX_DOCUMENTS]
    idx, counts, states, dead = await _index_facts(db, [d.id for d, _ in found])
    now = datetime.now(timezone.utc)
    rows = [_summary(d, s, counts.get(d.id, 0), states.get(d.id), idx, d.id in dead, now) for d, s in found]
    facets: dict[str, dict[str, int]] = {}
    for facet in FACETS:
        tally: dict[str, int] = {}
        for row in rows:
            if _matches(row, filters, skip=facet):  # a filter's own options ignore itself, so a second value can be added
                for value in _values(row, facet):
                    tally[value] = tally.get(value, 0) + 1
        facets[facet] = dict(sorted(tally.items()))
    shown = sorted((r for r in rows if _matches(r, filters)), key=SORTS.get(sort, SORTS["title"]), reverse=descending)
    return {"total": len(shown), "page": page, "size": size, "documents": shown[(page - 1) * size:page * size], "facets": facets, "capped": capped,
            "progress": _progress(rows, found), "index": {"model": idx.model_key if idx else None, "dimensions": idx.dimensions if idx else None, "mode": ("hybrid" if idx.dimensions else "keyword-only") if idx else None}}


def _progress(rows: list[dict], found: list) -> list[dict]:
    """Sync, indexing and embedding per source, as three separate answers: a finished sync says nothing about the other two."""
    sources: dict[str, dict] = {}
    for (_, s), row in zip(found, rows):
        p = sources.setdefault(str(s.id), {"id": str(s.id), "name": s.name, "type": s.type, "lifecycle": s.lifecycle, "documents": 0, "sync": _sync(s), "indexing": {}, "embedding": {}})
        p["documents"] += 1
        p["indexing"][row["indexing"]] = p["indexing"].get(row["indexing"], 0) + 1
        p["embedding"][row["embedding"]["state"]] = p["embedding"].get(row["embedding"]["state"], 0) + 1
    return sorted(sources.values(), key=lambda p: p["name"])


def _sync(s: KnowledgeSource) -> dict:
    cursor = s.sync_cursor or {}
    return {"listing": cursor.get("listing"), "content": cursor.get("content"), "status": s.last_sync_status, "remaining": cursor.get("remaining"), "failed": cursor.get("failed"),
            "last_attempt_at": _when(s.last_attempt_at), "last_success_at": _when(s.last_success_at), "next_sync_at": _when(s.next_sync_at)}


def _preview(chunks: list[Chunk]) -> tuple[str, bool]:
    """The stored text, put back together from its chunks by offset, so overlap between them is not shown twice."""
    out, end = [], None
    used = 0
    for c in chunks:
        body = c.text
        if end is not None and c.start_offset is not None and c.start_offset < end:
            body = body[end - c.start_offset:]
        elif end is not None:
            out.append("\n\n" if c.start_offset is None or c.start_offset > end + 1 else "\n")
        out.append(body)
        used += len(body)
        end = max(end or 0, c.end_offset or 0)
        if used >= PREVIEW_CHARS:
            return redact("".join(out))[:PREVIEW_CHARS], True
    return redact("".join(out)), False


async def detail(db: AsyncSession, principal: Principal, document_id: uuid.UUID) -> dict | None:
    pair = await get(db, principal, document_id)
    if pair is None:
        return None
    d, s = pair
    idx, counts, states, dead = await _index_facts(db, [d.id])
    now = datetime.now(timezone.utc)
    out = _summary(d, s, counts.get(d.id, 0), states.get(d.id), idx, d.id in dead, now)
    chunks = (await db.execute(select(Chunk).where(Chunk.document_id == d.id).order_by(Chunk.ord))).scalars().all()
    preview, cut = _preview(chunks)
    states_here = (await db.execute(select(DocumentIndexState, EmbeddingIndex).join(EmbeddingIndex, EmbeddingIndex.id == DocumentIndexState.index_id)
                                    .where(DocumentIndexState.document_id == d.id, EmbeddingIndex.status.in_(("active", "building", "retired"))).order_by(EmbeddingIndex.created_at.desc()))).all()
    replaced_by = await get(db, principal, d.superseded_by_id) if d.superseded_by_id else None
    replaces, params = visible(principal)
    earlier = (await db.execute(select(Document).join(KnowledgeSource, KnowledgeSource.id == Document.source_id).where(Document.superseded_by_id == d.id, replaces.bindparams(**params)).limit(20))).scalars().all()
    operator = principal.can(P.KNOWLEDGE_INDEX)
    out |= {"preview": preview, "preview_cut": cut, "url": service.safe_url(d.url), "citation": {"source": s.name, "source_type": s.type, "repository": (s.config or {}).get("repository"), "path": d.path, "revision": d.revision,
                                                                                               "content_changed_in": d.changed_revision, "url": service.safe_url(d.url), "pipeline_version": d.pipeline_version, "current_pipeline": PIPELINE_VERSION, "content_hash": d.content_hash,
                                                                                               "content_revision": d.content_revision, "indexed_revision": d.indexed_revision, "size_bytes": d.size_bytes},
            "review": {"trust": d.trust, "reviewed_at": _when(d.reviewed_at), "review_due_at": _when(d.review_due_at), "source_review_interval_days": s.review_interval_days, "source_last_reviewed_at": _when(s.last_reviewed_at),
                       "stale_policy": s.stale_policy, "stale_after_days": s.stale_after_days, "authority": s.authority, "shared_globally": s.shared_globally, "doc_state": d.doc_state},
            "generations": [{"index": i.index_name if operator else None, "status": i.status, "serving": idx is not None and i.id == idx.id, "model": st.model_key, "dimensions": st.dimensions, "chunks": st.chunks, "expected_chunks": st.expected_chunks,
                             "vectors": st.vectors, "expected_vectors": st.expected_vectors, "revision": st.revision, "current": st.revision == d.content_revision} for st, i in states_here],
            "superseded_by": ({"id": str(replaced_by[0].id), "title": replaced_by[0].title, "path": replaced_by[0].path} if replaced_by else {"path": d.superseded_by}) if d.superseded_by else None,
            "replaces": [{"id": str(o.id), "title": o.title, "path": o.path} for o in earlier],
            "can_review": principal.can(P.KNOWLEDGE_REVIEW, d.environment, d.namespace), "can_operate": operator}
    if operator:
        events = (await db.execute(select(IndexOutbox).where(IndexOutbox.document_id == d.id, IndexOutbox.status != "done").order_by(IndexOutbox.id.desc()).limit(20))).scalars().all()
        out["operations"] = {"vector_error": d.vector_error, "vector_attempts": d.vector_attempts, "vector_state": d.vector_state,
                             "generation_errors": [{"status": i.status, "error": st.error, "retries": st.retries} for st, i in states_here if st.error],
                             "events": [{"id": e.id, "op": e.op, "state": "retrying" if e.status == "pending" and e.attempts else e.status, "attempts": e.attempts, "error": e.error, "revision": e.revision} for e in events]}
    return out


async def chunks(db: AsyncSession, principal: Principal, document_id: uuid.UUID, offset: int = 0, limit: int = 20) -> dict | None:
    if await get(db, principal, document_id) is None:
        return None
    total = (await db.execute(select(func.count()).select_from(Chunk).where(Chunk.document_id == document_id))).scalar_one()
    rows = (await db.execute(select(Chunk).where(Chunk.document_id == document_id).order_by(Chunk.ord).offset(offset).limit(min(limit, PAGE_CHUNKS)))).scalars().all()
    return {"total": total, "offset": offset, "chunks": [{"id": str(c.id), "ord": c.ord, "heading": c.heading, "text": redact(c.text), "lines": [c.start_line, c.end_line], "offsets": [c.start_offset, c.end_offset], "tokens": c.token_estimate} for c in rows]}


async def counts(db: AsyncSession, principal: Principal) -> dict:
    """Totals for someone without index access: only what they can see."""
    clause, params = visible(principal)
    rows = (await db.execute(select(Document.status, func.count()).join(KnowledgeSource, KnowledgeSource.id == Document.source_id).where(clause.bindparams(**params)).group_by(Document.status))).all()
    clause, params = visible(principal)
    passages = (await db.execute(select(func.count()).select_from(Chunk).join(Document, Document.id == Chunk.document_id).join(KnowledgeSource, KnowledgeSource.id == Document.source_id).where(clause.bindparams(**params)))).scalar_one()
    return {"documents": dict(rows), "chunks": passages}
