"""Knowledge retrieval: indexing, versioned embedding indexes, hybrid search with access control.

PostgreSQL is the authority for content and access. An external index (OpenSearch, Pinecone) is a
derived copy, written only after the change has committed: a change adds a row to index_outbox in
the same transaction, and drain_outbox applies it later. So a rollback can never remove valid
content from search, and a crash replays rather than loses. Each event names a per-document
revision, which makes replays harmless and lets an older event be recognised and dropped.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..connectors.base import ConnectorError
from ..db import session_scope
from ..llm import budget
from ..llm.providers import LLMError, provider_for
from ..models import Chunk, Document, DocumentIndexState, EmbeddingIndex, IndexOutbox, KnowledgeSource, LLMConfig
from ..observability import RETRIEVAL_LATENCY
from ..rbac import Principal
from . import access
from .backends import OpenSearchBackend, PineconeBackend, PostgresBackend, rrf
from .chunking import MAX_CHUNKS, PIPELINE_VERSION, chunk, kind_of, lifecycle_of, title_of
from .secrets import clean, exclusion_reason

log = logging.getLogger("moks.rag")
EMBED_BATCH = 32
EMBED_BACKOFF_SECONDS = 5
REINDEX_EMBED_TRIES = 7  # a reindex is the one place worth waiting out a provider's rate limit
INDEX_LOCK = 7_203_114  # advisory lock: creating the first index
BUILD_STALE = timedelta(minutes=30)
OUTBOX_MAX_ATTEMPTS = 8
OUTBOX_MAX_BACKOFF_SECONDS = 3600
CATCHUP_PASSES = 20
_opensearch: OpenSearchBackend | None = None
_pinecone: PineconeBackend | None = None


def opensearch() -> OpenSearchBackend:
    global _opensearch
    if _opensearch is None:
        s = get_settings()
        _opensearch = OpenSearchBackend(s.opensearch_url, s.opensearch_username, s.opensearch_password, s.opensearch_verify_tls)
    return _opensearch


def pinecone() -> PineconeBackend:
    global _pinecone
    if _pinecone is None:
        s = get_settings()
        _pinecone = PineconeBackend(os.environ.get("MOKS_CRED_PINECONE_API_KEY"), s.pinecone_cloud, s.pinecone_region)
    return _pinecone


def external(name: str | None = None):
    """The index outside PostgreSQL, if any. Both kinds offer the same index lifecycle."""
    name = name or backend_name()
    return opensearch() if name == "opensearch" else pinecone() if name == "pinecone" else None


def backend_name() -> str:
    return get_settings().rag_backend


async def embedding_config(db: AsyncSession) -> LLMConfig | None:
    return (await db.execute(select(LLMConfig).where(LLMConfig.purpose == "embedding", LLMConfig.is_active.is_(True)))).scalar_one_or_none()


def model_key(cfg: LLMConfig | None) -> str:
    return f"{cfg.provider}/{cfg.model}/{cfg.embedding_dimensions or 'default'}" if cfg else "keyword-only"


async def active_index(db: AsyncSession) -> EmbeddingIndex | None:
    return (await db.execute(select(EmbeddingIndex).where(EmbeddingIndex.backend == backend_name(),
                                                          EmbeddingIndex.status == "active"))).scalar_one_or_none()


async def embedding_dimensions(cfg: LLMConfig) -> int:
    """The model's real output size. A configured size must agree: a vector mapping cannot change."""
    vec = (await budget.embed(provider_for(cfg), cfg, ["dimension probe"], purpose="dimension_probe"))[0]
    if cfg.embedding_dimensions and cfg.embedding_dimensions != len(vec):
        raise ConnectorError("config", f"{cfg.name} is configured for {cfg.embedding_dimensions} dimensions but returns {len(vec)}")
    return len(vec)


async def ensure_index(db: AsyncSession) -> EmbeddingIndex:
    """The active index for the current backend; created when none exists, with its vector size
    resolved first. If the embedding model cannot answer, the index is keyword-only and says so;
    a later reindex adds vectors. A model change is handled by reindex(), not here."""
    idx = await active_index(db)
    if idx:
        return idx
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": INDEX_LOCK})  # two workers' first documents
    idx = await active_index(db)
    if idx:
        return idx
    cfg = await embedding_config(db)
    if backend_name() == "postgres" and cfg and not await PostgresBackend(db).has_vector():
        cfg = None  # pgvector not installed: keyword-only
    dims, error = None, None
    if cfg:
        try:
            dims = await embedding_dimensions(cfg)
        except (LLMError, ConnectorError) as exc:
            error = f"embedding model unavailable: {getattr(exc, 'message', exc)}"
            log.warning("index created keyword-only", extra={"event": "embedding_failed", "errorType": type(exc).__name__})
            cfg = None
    idx = EmbeddingIndex(backend=backend_name(), llm_config_id=cfg.id if cfg else None, model_key=model_key(cfg),
                         dimensions=dims, index_name="", status="active", activated_at=datetime.now(timezone.utc), error=error)
    db.add(idx)
    await db.flush()
    idx.index_name = f"moks-chunks-{str(idx.id)[:8]}"
    if external():  # idempotent, and an empty index left by a rollback holds nothing
        await external().ensure_index(idx.index_name, idx.dimensions)
    return idx


async def _embed_batch(provider, cfg: LLMConfig, texts: list[str], tries: int) -> list[list[float]]:
    for attempt in range(tries):
        try:
            return await budget.embed(provider, cfg, texts, purpose="indexing")
        except LLMError as exc:
            if exc.kind != "rate_limited" or attempt == tries - 1:
                raise
            await asyncio.sleep(EMBED_BACKOFF_SECONDS * 2 ** attempt)
    raise LLMError("rate_limited", "embedding retries exhausted")


async def _embed(db: AsyncSession, idx: EmbeddingIndex, texts: list[str], tries: int = 2) -> list[list[float]] | None:
    if not idx.llm_config_id or not idx.dimensions:
        return None  # keyword-only index: a vector field does not exist to write to
    cfg = await db.get(LLMConfig, idx.llm_config_id)
    if cfg is None:
        return None
    provider = provider_for(cfg)
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        out.extend(await _embed_batch(provider, cfg, texts[i:i + EMBED_BATCH], tries))
    if any(len(v) != idx.dimensions for v in out):
        raise LLMError("bad_response", f"embedding size differs from the index's {idx.dimensions} dimensions; reindex after a model change")
    return out


def _row(c: Chunk, d: Document, src: KnowledgeSource) -> dict:
    return {"chunk_id": str(c.id), "document_id": str(d.id), "environment": d.environment, "namespace": d.namespace,
            "repository": (src.config or {}).get("repository") or src.name, "service": d.service or "", "path": d.path,
            "heading": c.heading, "text": c.text}


CHUNK_NS = uuid.UUID("9c1a7b52-6d7e-4b0a-9f55-2f6f6c3f2a11")


def chunk_id(document_id: uuid.UUID, content_hash: str, ordinal: int) -> uuid.UUID:
    """The same document, content, pipeline and position always give one id, so a replay cannot duplicate
    a chunk, and a new version never reuses an old one's id."""
    return uuid.uuid5(CHUNK_NS, f"{document_id}:{content_hash}:{PIPELINE_VERSION}:{ordinal}")


async def exclude_document(db: AsyncSession, src: KnowledgeSource, path: str, reason: str, content_hash: str, revision: str | None) -> Document:
    """Recorded without text, and any earlier version stops being searchable."""
    doc = (await db.execute(select(Document).where(Document.source_id == src.id, Document.path == path))).scalar_one_or_none()
    if doc is None:
        doc = Document(source_id=src.id, path=path, content_hash=content_hash, content_revision=0, indexed_revision=0, environment=src.scope_environment, namespace=src.scope_namespace)
        db.add(doc)
    elif doc.status == "excluded" and doc.excluded_reason == reason and doc.content_hash == content_hash:
        doc.checked_at = datetime.now(timezone.utc)
        return doc
    await _drop_chunks(db, doc)
    doc.content_hash, doc.revision, doc.status, doc.excluded_reason, doc.title = content_hash, revision, "excluded", reason, path.rsplit("/", 1)[-1]
    doc.pipeline_version, doc.updated_at, doc.checked_at, doc.vector_state = PIPELINE_VERSION, datetime.now(timezone.utc), datetime.now(timezone.utc), "not_applicable"
    await db.flush()
    await _record_change(db, doc, "delete")
    return doc


async def index_document(db: AsyncSession, src: KnowledgeSource, path: str, raw: str, *, revision: str | None,
                         url: str | None, content_hash: str | None = None, extra: dict | None = None) -> Document:
    """Create or replace one document and its chunks. Excluded content is recorded without text."""
    extra = extra or {}
    content_hash = content_hash or hashlib.sha256(raw.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    doc = (await db.execute(select(Document).where(Document.source_id == src.id, Document.path == path))).scalar_one_or_none()
    if doc and doc.content_hash == content_hash and doc.status == "active" and doc.pipeline_version == PIPELINE_VERSION:
        doc.checked_at, doc.revision = now, revision or doc.revision
        return doc  # unchanged: incremental sync skips it
    content_changed = doc is None or doc.content_hash != content_hash
    if doc is None:
        doc = Document(source_id=src.id, path=path, content_hash=content_hash, content_revision=0, indexed_revision=0)
        db.add(doc)
    await _drop_chunks(db, doc)
    doc.content_hash, doc.revision, doc.url = content_hash, revision, url
    if content_changed:
        doc.changed_revision = revision
    doc.kind = kind_of(path)
    doc.environment = extra.get("environment", src.scope_environment)
    doc.namespace = extra.get("namespace", src.scope_namespace)
    doc.service, doc.owner = extra.get("service", src.service), extra.get("owner", src.owner)
    doc.size_bytes = len(raw.encode())
    doc.updated_at, doc.checked_at = now, now
    doc.pipeline_version = PIPELINE_VERSION
    reason = exclusion_reason(path, raw)
    text_clean = clean(raw) if not reason else ""
    pieces = chunk(path, text_clean) if not reason else []
    if len(pieces) > MAX_CHUNKS:
        reason, pieces = f"would make {len(pieces)} chunks, over the {MAX_CHUNKS} limit", []
    if reason:
        doc.status, doc.excluded_reason, doc.title, doc.vector_state = "excluded", reason, path.rsplit("/", 1)[-1], "not_applicable"
        await db.flush()
        await _record_change(db, doc, "delete")
        return doc
    doc.status, doc.excluded_reason, doc.title = "active", None, extra.get("title") or title_of(path, text_clean)
    doc.doc_state, doc.superseded_by = lifecycle_of(text_clean)
    doc.vector_state, doc.vector_error = "unknown", None
    await db.flush()
    chunks = [Chunk(id=chunk_id(doc.id, content_hash, i), document_id=doc.id, ord=i, heading=p.heading[:480], text=p.text, start_line=p.start_line, end_line=p.end_line,
                    start_offset=p.start_offset, end_offset=p.end_offset, token_estimate=p.tokens, text_hash=p.text_hash) for i, p in enumerate(pieces)]
    db.add_all(chunks)
    await db.flush()
    await _record_change(db, doc, "upsert")
    if external():
        return doc  # applied after commit by drain_outbox
    idx = await ensure_index(db)
    await _write_postgres(db, idx, doc, src, chunks)
    doc.indexed_at = datetime.now(timezone.utc)
    return doc


def _note_vectors(doc: Document, idx: EmbeddingIndex, written: bool, error: str | None) -> None:
    """A document indexed without its vectors is not fully indexed: it stays eligible for backfill."""
    doc.vector_state = "not_applicable" if not idx.dimensions else "complete" if written else "missing"
    doc.vector_error = error


async def _note_index(db: AsyncSession, doc_id: uuid.UUID, idx: EmbeddingIndex, revision: int, chunks: int, vectors: int) -> None:
    await db.execute(insert(DocumentIndexState).values(document_id=doc_id, index_id=idx.id, revision=revision, chunks=chunks, vectors=vectors).on_conflict_do_update(
        index_elements=["document_id", "index_id"], set_={"revision": revision, "chunks": chunks, "vectors": vectors, "updated_at": func.now()}))


async def _try_embed(db: AsyncSession, idx: EmbeddingIndex, chunks: list[Chunk]) -> tuple[list[list[float]] | None, str | None]:
    try:
        return (await _embed(db, idx, [f"{c.heading}\n{c.text}" for c in chunks]) if chunks else None), None
    except LLMError as exc:
        log.warning("embedding failed; indexed keyword-only", extra={"event": "embedding_failed", "errorType": exc.kind})
        return None, f"{exc.kind}: {exc.message}"[:300]


async def _write_postgres(db: AsyncSession, idx: EmbeddingIndex, doc: Document, src: KnowledgeSource, chunks: list[Chunk]) -> None:
    embeddings, error = await _try_embed(db, idx, chunks)
    await PostgresBackend(db).index_chunks(idx, [_row(c, doc, src) for c in chunks], embeddings)
    _note_vectors(doc, idx, embeddings is not None, error)
    await _note_index(db, doc.id, idx, doc.content_revision, len(chunks), len(embeddings or []))


async def _record_change(db: AsyncSession, doc: Document, op: str) -> None:
    """Part of the caller's transaction: the event exists exactly when the change does."""
    doc.content_revision = (doc.content_revision or 0) + 1
    if external():
        db.add(IndexOutbox(document_id=doc.id, revision=doc.content_revision, op=op))
    else:
        doc.indexed_revision = doc.content_revision  # the same database, so already consistent
    await db.flush()


async def _drop_chunks(db: AsyncSession, doc: Document) -> None:
    if doc.id is not None:
        await db.execute(delete(Chunk).where(Chunk.document_id == doc.id))


async def remove_document(db: AsyncSession, doc: Document) -> None:
    """Search stops returning it at commit, because results are checked against PostgreSQL. The
    external copy goes when the outbox event is applied."""
    await _drop_chunks(db, doc)
    doc.status, doc.updated_at = "deleted", datetime.now(timezone.utc)
    await _record_change(db, doc, "delete")


async def _write_document(db: AsyncSession, idx: EmbeddingIndex, document_id: uuid.UUID, revision: int | None = None) -> None:
    """Make one external index match PostgreSQL for one document. Replaying this is harmless."""
    ext = external()
    await ext.delete_document(idx.index_name, document_id)
    row = (await db.execute(select(Document, KnowledgeSource).join(KnowledgeSource, KnowledgeSource.id == Document.source_id).where(Document.id == document_id))).first()
    if row is None or row[0].status != "active":
        await db.execute(delete(DocumentIndexState).where(DocumentIndexState.document_id == document_id, DocumentIndexState.index_id == idx.id))
        return
    doc, src = row
    chunks = (await db.execute(select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.ord))).scalars().all()
    embeddings, error = await _try_embed(db, idx, chunks)
    if chunks:
        await ext.index_chunks(idx.index_name, [_row(c, doc, src) for c in chunks], embeddings)
    _note_vectors(doc, idx, embeddings is not None, error)
    await _note_index(db, doc.id, idx, revision or doc.content_revision, len(chunks), len(embeddings or []))


async def _lock_document(db: AsyncSession, document_id: uuid.UUID) -> bool:
    """Held to the end of the transaction, so one document is only ever written by one worker."""
    return (await db.execute(text("SELECT pg_try_advisory_xact_lock(hashtextextended(:d, 7203))"), {"d": str(document_id)})).scalar_one()


async def _apply_event(db: AsyncSession, ev: IndexOutbox) -> str:
    now = (await db.execute(select(func.now()))).scalar_one()
    event_id = ev.id
    if not await _lock_document(db, ev.document_id):
        ev.next_attempt_at = now + timedelta(seconds=5)  # an older event for this document is still writing
        return "deferred"
    doc = await db.get(Document, ev.document_id, populate_existing=True)
    if doc is not None and (ev.revision < doc.content_revision or ev.revision <= doc.indexed_revision):
        ev.status, ev.processed_at, ev.error = "done", now, "superseded"  # newer content has its own event, or this is a replay
        return "skipped"
    try:
        idx = await ensure_index(db)
        await _write_document(db, idx, ev.document_id, ev.revision)
    except Exception as exc:
        await db.rollback()
        return await _retry(event_id, exc)
    current = (await db.execute(select(Document.content_revision).where(Document.id == ev.document_id).execution_options(populate_existing=True))).scalar_one_or_none()
    ev.status, ev.processed_at, ev.error = "done", now, None
    if doc is not None and current == ev.revision:
        doc.indexed_revision, doc.indexed_at = ev.revision, now
    elif doc is not None:
        ev.error = "superseded during write"  # the newer event runs after this lock is released, and rewrites from PostgreSQL
    return "applied"


async def _retry(event_id: int, exc: Exception) -> str:
    async with session_scope() as db:
        ev = await db.get(IndexOutbox, event_id, with_for_update=True)
        now = (await db.execute(select(func.now()))).scalar_one()
        ev.attempts += 1
        ev.error = f"{type(exc).__name__}: {str(getattr(exc, 'message', exc))[:300]}"
        if ev.attempts >= OUTBOX_MAX_ATTEMPTS:
            ev.status, ev.processed_at = "dead", now
            log.error("index event gave up", extra={"event": "index_outbox_dead", "errorType": type(exc).__name__})
            return "dead"
        ev.next_attempt_at = now + timedelta(seconds=min(30 * 2 ** (ev.attempts - 1), OUTBOX_MAX_BACKOFF_SECONDS))
        return "retry"


async def drain_outbox(limit: int = 100) -> dict[str, int]:
    """Apply committed changes in order. Each event is its own transaction holding its row lock, so
    workers never apply the same event twice at once, and a crash leaves it pending."""
    counts = {"applied": 0, "skipped": 0, "retry": 0, "dead": 0, "deferred": 0}
    if not external():
        return counts
    for _ in range(limit):
        async with session_scope() as db:
            ev = (await db.execute(select(IndexOutbox).where(IndexOutbox.status == "pending", IndexOutbox.next_attempt_at <= func.now())
                                   .order_by(IndexOutbox.id).limit(1).with_for_update(skip_locked=True))).scalars().first()
            if ev is None:
                break
            counts[await _apply_event(db, ev)] += 1
    return counts


async def reconcile_index() -> int:
    """Finds documents PostgreSQL and the index disagree on with nothing pending to fix it, for
    example after an event went dead and was then cleared, and queues them again."""
    if not external():
        return 0
    async with session_scope() as db:
        rows = (await db.execute(select(Document).where(Document.content_revision != Document.indexed_revision, ~select(IndexOutbox.id).where(
            IndexOutbox.document_id == Document.id, IndexOutbox.status.in_(("pending", "dead"))).exists()).limit(500))).scalars().all()
        for doc in rows:
            await _record_change(db, doc, "upsert" if doc.status == "active" else "delete")
        return len(rows)


async def verify_index(sample: int = 50) -> dict[str, int]:
    """Revisions say what should be there; this asks the index. A sample of documents whose chunk count
    differs is queued again. Pinecone cannot count by metadata, so there this checks nothing and says so."""
    result = {"checked": 0, "mismatched": 0, "unsupported": 0}
    if not external():
        return result
    async with session_scope() as db:
        idx = await active_index(db)
        if idx is None:
            return result
        docs = (await db.execute(select(Document).where(Document.status.in_(("active", "deleted", "excluded")), Document.content_revision == Document.indexed_revision).order_by(func.random()).limit(sample))).scalars().all()
        for doc in docs:
            found = await external().count_document(idx.index_name, doc.id)
            if found is None:
                result["unsupported"] += 1
                continue
            want = (await db.execute(select(func.count()).select_from(Chunk).where(Chunk.document_id == doc.id))).scalar_one() if doc.status == "active" else 0
            result["checked"] += 1
            if found != want:
                result["mismatched"] += 1
                await _record_change(db, doc, "upsert" if doc.status == "active" else "delete")
    return result


async def backfill_vectors(limit: int = 20) -> dict[str, int]:
    """Documents that were indexed while embedding failed get their vectors once it works again. Stops
    at the first failure: the provider is still down, and each try costs budget."""
    done = {"filled": 0, "failed": 0}
    for _ in range(limit):
        async with session_scope() as db:
            idx = await active_index(db)
            if idx is None or not idx.dimensions:
                return done
            doc = (await db.execute(select(Document).where(Document.status == "active", Document.vector_state.in_(("missing", "unknown")), Document.content_revision == Document.indexed_revision)
                                    .order_by(Document.updated_at).limit(1).with_for_update(skip_locked=True))).scalars().first()
            if doc is None or not await _lock_document(db, doc.id):
                return done
            if external():
                await _write_document(db, idx, doc.id)
            else:
                src = await db.get(KnowledgeSource, doc.source_id)
                await _write_postgres(db, idx, doc, src, (await db.execute(select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.ord))).scalars().all())
            if doc.vector_state != "complete":
                done["failed"] += 1
                return done
            done["filled"] += 1
    return done


async def retry_dead(document_id: uuid.UUID | None = None, limit: int = 200) -> int:
    """Controlled: one document, or a bounded number, so a retry cannot flood a recovering index."""
    async with session_scope() as db:
        q = select(IndexOutbox.id).where(IndexOutbox.status == "dead").order_by(IndexOutbox.id).limit(limit)
        if document_id:
            q = q.where(IndexOutbox.document_id == document_id)
        rows = (await db.execute(update(IndexOutbox).where(IndexOutbox.id.in_(q)).values(status="pending", attempts=0, next_attempt_at=func.now(), processed_at=None).returning(IndexOutbox.id))).all()
        return len(rows)


async def _index_batch(db: AsyncSession, new: EmbeddingIndex, batch: list) -> None:
    rows = [_row(c, d, src) for c, d, src in batch]
    embs = await _embed(db, new, [f"{r['heading']}\n{r['text']}" for r in rows], tries=REINDEX_EMBED_TRIES)
    if external():
        await external().index_chunks(new.index_name, rows, embs)
    else:
        await PostgresBackend(db).index_chunks(new, rows, embs)


async def _pending_rows(db: AsyncSession, done: set) -> list:
    rows = (await db.execute(select(Chunk, Document, KnowledgeSource).join(Document, Document.id == Chunk.document_id)
                             .join(KnowledgeSource, KnowledgeSource.id == Document.source_id)
                             .where(Document.status == "active").order_by(Chunk.id))).all()
    return [r for r in rows if r[0].id not in done]


async def _catch_up(db: AsyncSession, new: EmbeddingIndex, caught: int) -> int:
    """Re-copy from PostgreSQL every document changed since `caught`, until a pass finds none and
    the switch can happen with the outbox held still."""
    for _ in range(CATCHUP_PASSES):
        high = (await db.execute(select(func.coalesce(func.max(IndexOutbox.id), 0)))).scalar_one()
        ids = (await db.execute(select(IndexOutbox.document_id).where(IndexOutbox.id > caught).distinct())).scalars().all()
        if not ids:
            return caught
        for document_id in ids:
            await _write_document(db, new, document_id)
        caught = high
        new.heartbeat_at = datetime.now(timezone.utc)
        await db.commit()
    raise ConnectorError("conflict", "changes kept arriving faster than the reindex could catch up; it will need another run")


async def reindex(db: AsyncSession, job_id: uuid.UUID | None = None) -> dict:
    """Build a new index version beside the active one, then switch in one committed transaction.
    Search uses the old index until that commit. A failed build leaves the old index active. The
    old physical index is deleted only after the switch has committed (cleanup_retired).

    Changes during a build: the outbox position is noted first, the copy reads PostgreSQL, and every
    document changed after that position is copied again. Inserts into the outbox are held only for
    the moment of the final check and the switch, so nothing can slip between them."""
    cfg = await embedding_config(db)
    if backend_name() == "postgres" and cfg and not await PostgresBackend(db).has_vector():
        raise ConnectorError("config", "pgvector is not installed in this PostgreSQL; use the opensearch backend or a pgvector image")
    await db.execute(update(EmbeddingIndex).where(
        EmbeddingIndex.backend == backend_name(), EmbeddingIndex.status == "building",
        EmbeddingIndex.heartbeat_at < datetime.now(timezone.utc) - BUILD_STALE).values(status="failed", error="build stopped responding", cleanup_pending=True))
    await db.commit()
    dims = await embedding_dimensions(cfg) if cfg else None
    new = EmbeddingIndex(backend=backend_name(), llm_config_id=cfg.id if cfg else None, model_key=model_key(cfg),
                         dimensions=dims, index_name="", status="building", heartbeat_at=datetime.now(timezone.utc), build_job_id=job_id,
                         build_watermark=(await db.execute(select(func.coalesce(func.max(IndexOutbox.id), 0)))).scalar_one())
    db.add(new)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise ConnectorError("conflict", "a reindex is already running") from None
    new.index_name = f"moks-chunks-{str(new.id)[:8]}"
    await db.commit()  # visible and recoverable from here on
    new_id = new.id
    done: set = set()
    try:
        if external():
            await external().ensure_index(new.index_name, new.dimensions)
        while True:  # the second pass catches documents indexed while the first ran
            rows = await _pending_rows(db, done)
            if not rows:
                break
            for i in range(0, len(rows), 200):
                batch = rows[i:i + 200]
                await _index_batch(db, new, batch)
                done.update(r[0].id for r in batch)
                new.heartbeat_at, new.chunk_count = datetime.now(timezone.utc), len(done)  # also the progress an operator sees
                await db.commit()
        if external():
            caught = new.build_watermark or 0
            while True:
                caught = await _catch_up(db, new, caught)
                await db.execute(text("LOCK TABLE index_outbox IN EXCLUSIVE MODE"))  # held to the commit below
                if (await db.execute(select(func.coalesce(func.max(IndexOutbox.id), 0)))).scalar_one() <= caught:
                    break
                await db.rollback()
    except Exception as exc:
        await db.rollback()
        new = await db.get(EmbeddingIndex, new_id)
        new.status, new.error, new.cleanup_pending = "failed", str(getattr(exc, "message", exc))[:500], True
        await db.commit()
        raise
    old = await active_index(db)
    old_name = old.index_name if old else None
    if old:
        old.status, old.cleanup_pending = "retired", True
        await db.flush()
    new.status, new.activated_at, new.chunk_count = "active", datetime.now(timezone.utc), len(done)
    await db.execute(update(Document).where(Document.status == "active").values(vector_state="complete" if new.dimensions else "not_applicable", vector_error=None))  # _index_batch fails the build if any embedding fails
    result = {"index": new.index_name, "model": new.model_key, "chunks": len(done), "replaced": old_name}
    await db.commit()
    return result


async def cleanup_retired() -> int:
    """Delete physical indexes of retired or failed versions. Safe to retry: the row keeps
    cleanup_pending until the delete succeeds, and the active index is never selected."""
    async with session_scope() as db:
        rows = (await db.execute(select(EmbeddingIndex).where(EmbeddingIndex.cleanup_pending.is_(True), EmbeddingIndex.status != "active",
                                                              EmbeddingIndex.status != "building").with_for_update(skip_locked=True))).scalars().all()
        done = 0
        for idx in rows:
            try:
                if idx.backend in ("opensearch", "pinecone") and idx.index_name:
                    await external(idx.backend).drop_index(idx.index_name)
                elif idx.backend == "postgres" and await PostgresBackend(db).has_vector():
                    await db.execute(text("DELETE FROM chunk_embeddings WHERE index_id = :i"), {"i": idx.id})
            except ConnectorError as exc:
                log.warning("index cleanup will retry", extra={"event": "index_cleanup_failed", "errorType": exc.kind})
                continue
            idx.cleanup_pending = False
            done += 1
        return done


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9]*(?:[-_.:/][A-Za-z0-9]+)+|[A-Za-z]+[0-9]+[A-Za-z0-9]*|[0-9]+[A-Za-z]+[A-Za-z0-9]*|[a-z]+[A-Z][A-Za-z0-9]*")
WORD = re.compile(r"[a-z0-9]{3,}")
STOP = set("the and for are was why how what when with from that this does did has have not can you our into any all its show get".split())
MAX_POOL = 400
PER_DOCUMENT = 3
NEAR_DUPLICATE = 0.85
CONTEXT_TOKENS = 500
SCORE_NOTE = "score is a relative ranking within this search, not a probability or a measure of confidence"


def identifiers(query: str) -> list[str]:
    return list(dict.fromkeys(m.group(0) for m in IDENTIFIER.finditer(query) if len(m.group(0)) >= 4))


def _terms(query: str) -> set[str]:
    return {w for w in WORD.findall(query.lower()) if w not in STOP}


def _freshness(s: KnowledgeSource, d: Document, now: datetime) -> tuple[str, str | None]:
    """current | stale | superseded | deprecated, and why. Age proves nothing unless the source says how old is too old."""
    if d.doc_state == "superseded":
        return "superseded", f"replaced by {d.superseded_by}" if d.superseded_by else "marked superseded"
    if d.doc_state == "deprecated":
        return "deprecated", "marked deprecated in the document"
    if s.review_interval_days and (s.last_reviewed_at is None or now - s.last_reviewed_at > timedelta(days=s.review_interval_days)):
        return "stale", f"the source's {s.review_interval_days}-day review is overdue"
    if s.stale_after_days and (d.checked_at is None or now - d.checked_at > timedelta(days=s.stale_after_days)):
        return "stale", f"not confirmed against its source in {s.stale_after_days} days"
    return "current", None


def _shingles(text_: str) -> set[str]:
    words = WORD.findall(text_.lower())
    return {" ".join(words[i:i + 3]) for i in range(max(len(words) - 2, 1))}


def _is_near_duplicate(a: set[str], b: set[str]) -> bool:
    return bool(a and b) and len(a & b) / len(a | b) >= NEAR_DUPLICATE


def _judge(query: str, tokens: list[str], results: list[dict]) -> dict:
    """Whether the passages could answer this, from what they literally contain. Deterministic, and
    deliberately not derived from fused scores, which rank but do not measure."""
    if not results:
        return {"state": "none", "reason": "No readable document matched. Nothing here says it is fine or broken."}
    body = " ".join(f"{r['title']} {r['heading']} {r['text']} {r['citation']['path']}" for r in results[:3]).lower()
    if tokens and not any(t.lower() in body for t in tokens):
        return {"state": "weak", "reason": f"No passage contains {', '.join(tokens[:3])}, so these are only loosely related."}
    terms = _terms(query)
    covered = sum(t in body for t in terms)
    if terms and not tokens and covered / len(terms) < 0.4:
        return {"state": "weak", "reason": "The passages share few of the question's terms."}
    return {"state": "supported", "reason": "The passages contain the terms searched for. That is not proof they answer the question."}


async def _rankings(db: AsyncSession, query: str, tokens: list[str], found: list, pool: int, idx: EmbeddingIndex | None, qv: list[float] | None) -> tuple[list[list[str]], bool, str | None]:
    """Each ranker's candidates, whether every one of them came through a complete filter, and why any fell back."""
    rankings, exact, reason = [], True, None
    pg = PostgresBackend(db)
    if backend_name() == "opensearch":
        try:
            ranked, ok = await opensearch().keyword(idx.index_name, query, found, pool)
            rankings.append(ranked)
            exact = exact and ok
        except ConnectorError as exc:
            reason = f"OpenSearch unavailable, keyword search served by PostgreSQL ({exc.kind})"
            rankings.append(await pg.keyword(query, found, pool))
    else:
        rankings.append(await pg.keyword(query, found, pool))
    if tokens:
        ranked = await pg.identifiers(tokens, found, pool)
        rankings += [ranked, ranked]  # an exact identifier outweighs any other single signal
    if qv is not None and idx is not None:
        if external():
            try:
                ranked, ok = await external().vector(idx.index_name, qv, found, pool)
                rankings.append(ranked)
                exact = exact and ok
            except ConnectorError as exc:
                reason = reason or f"vector search unavailable ({exc.kind}), so this is keyword only"
        else:
            rankings.append(await pg.vector(qv, idx.id, found, pool))
    return rankings, exact, reason


async def _query_vector(db: AsyncSession, principal: Principal, query: str, idx: EmbeddingIndex | None) -> tuple[list[float] | None, str | None]:
    if idx is None:
        return None, "nothing has been indexed yet"
    cfg = await embedding_config(db)
    if cfg is None:
        return None, "no embedding model is active"
    if not idx.dimensions:
        return None, "this index has no vectors; run a reindex"
    if idx.llm_config_id != cfg.id or idx.model_key != model_key(cfg):
        return None, "the active embedding model differs from the index; run a reindex"
    if not external() and not await PostgresBackend(db).has_vector():
        return None, "pgvector is not installed"
    try:
        return (await budget.embed(provider_for(cfg), cfg, [query], user_id=uuid.UUID(principal.user_id), purpose="search", wait_seconds=2))[0], None
    except (LLMError, ConnectorError) as exc:
        log.warning("vector search unavailable", extra={"event": "vector_search_failed", "errorType": type(exc).__name__})
        return None, f"embedding failed ({getattr(exc, 'kind', 'error')}), so this is keyword only"


async def search(db: AsyncSession, principal: Principal, query: str, k: int = 8, environment: str | None = None,
                 namespace: str | None = None, service: str | None = None, neighbors: bool = False) -> dict:
    start = time.monotonic()
    environment, namespace = environment or None, None if namespace in (None, "*") else namespace
    found = access.grants(principal, environment, namespace)
    empty = {"results": [], "evidence": _judge(query, [], []), "score_note": SCORE_NOTE}
    if not found:
        return {**empty, "mode": "none", "note": "you have no knowledge access for this scope"}
    idx = await active_index(db)
    if backend_name() == "opensearch" and idx is None:
        return {**empty, "mode": "empty", "note": "nothing has been indexed yet"}
    tokens = identifiers(query)
    qv, reason = await _query_vector(db, principal, query, idx)
    now = datetime.now(timezone.utc)
    pool = max(k * 4, 20)
    expanded = 0
    while True:
        rankings, exact, backend_reason = await _rankings(db, query, tokens, found, pool, idx, qv)
        reason = backend_reason or reason
        mode = "hybrid" if qv is not None and not backend_reason else "keyword"
        fused = rrf(*rankings)
        ids = [uuid.UUID(cid) for cid, _ in fused]
        rows = (await db.execute(select(Chunk, Document, KnowledgeSource).join(Document, Document.id == Chunk.document_id)
                                 .join(KnowledgeSource, KnowledgeSource.id == Document.source_id)
                                 .where(Chunk.id.in_(ids), Document.status == "active", KnowledgeSource.lifecycle == "active"))).all() if ids else []
        by_id = {str(c.id): (c, d, s) for c, d, s in rows}
        allowed = []
        for cid, score in fused:
            if cid not in by_id:
                continue
            c, d, s = by_id[cid]
            # Authoritative: what the index said is only a suggestion.
            if not access.can_read(principal, d.environment, d.namespace) or not access.in_request(environment, namespace, d.environment, d.namespace):
                continue
            state, why = _freshness(s, d, now)
            if state == "stale" and s.stale_policy == "exclude":
                continue
            allowed.append((cid, score, c, d, s, state, why))
        full = max(len(r) for r in rankings) if rankings else 0
        if len(allowed) >= k * 2 or full < pool or pool >= MAX_POOL:
            break  # enough survived, or there is nothing further down
        pool = min(pool * 3, MAX_POOL)  # unreadable or retired documents crowded this page
        expanded += 1
    results = _rerank(allowed, query, tokens, service, k, now)
    if neighbors:
        await _attach_neighbors(db, results)
    _flag_conflicts(results)
    RETRIEVAL_LATENCY.labels(backend_name()).observe(time.monotonic() - start)
    out = {"results": results, "mode": mode, "index": idx.index_name if idx else None, "evidence": _judge(query, tokens, results), "score_note": SCORE_NOTE,
           "candidates": {"filtered_before_ranking": exact, "expanded": expanded, "considered": len(allowed)}}
    if reason:
        out["fallback_reason"] = reason
    return out


def _rerank(allowed: list, query: str, tokens: list[str], service: str | None, k: int, now: datetime) -> list[dict]:
    """After authorization, so nothing unreadable can influence it. Small, explainable adjustments
    on top of the fused rank: enough to break near ties, not enough to bury a clear match."""
    wanted = {service.lower()} if service else set()
    terms = _terms(query)
    scored = []
    for cid, score, c, d, s, state, why in allowed:
        signals: dict[str, float] = {}
        hay = f"{c.text} {c.heading} {d.path}".lower()
        hit = [t for t in tokens if t.lower() in hay]
        if hit:
            signals["exact_identifier"] = 0.03 * len(hit)
        names = {x.lower() for x in [d.service, *((s.applies_to or {}).get("services") or [])] if x}
        if names & (wanted | terms):
            signals["service"] = 0.015
        if s.authority:
            signals["authority"] = 0.003 * s.authority
        if state == "stale":
            signals["stale"] = -0.01
        elif state in ("superseded", "deprecated"):
            signals["retired"] = -0.03
        scored.append((score + sum(signals.values()), signals, cid, c, d, s, state, why))
    scored.sort(key=lambda t: (-t[0], t[2]))
    results: list[dict[str, Any]] = []
    per_doc: dict[uuid.UUID, int] = {}
    seen_hash: set[str] = set()
    seen_shingles: list[set[str]] = []
    dropped = 0
    for rank, signals, cid, c, d, s, state, why in scored:
        shingles = _shingles(c.text)
        if per_doc.get(d.id, 0) >= PER_DOCUMENT or (c.text_hash and c.text_hash in seen_hash) or any(_is_near_duplicate(shingles, x) for x in seen_shingles):
            dropped += 1
            continue
        per_doc[d.id] = per_doc.get(d.id, 0) + 1
        seen_shingles.append(shingles)
        if c.text_hash:
            seen_hash.add(c.text_hash)
        results.append({
            "chunk_id": cid, "document_id": str(d.id), "ord": c.ord, "score": round(rank, 5), "signals": signals, "title": d.title, "heading": c.heading, "text": c.text,
            "citation": {"source": s.name, "source_type": s.type, "repository": (s.config or {}).get("repository"), "path": d.path, "revision": d.revision,
                         "lines": [c.start_line, c.end_line], "offsets": [c.start_offset, c.end_offset], "url": _line_url(d.url, c.start_line, c.end_line)},
            "scope": {"environment": d.environment, "namespace": d.namespace, "service": d.service, "owner": d.owner, "team": s.team, "classification": s.classification},
            "freshness": {"state": state, "reason": why, "last_checked": d.checked_at.isoformat() if d.checked_at else None, "content_changed_in": d.changed_revision,
                          "document_updated": d.updated_at.isoformat() if d.updated_at else None, "source_synced": s.last_sync_at.isoformat() if s.last_sync_at else None},
        })
        if len(results) >= k:
            break
    return results


async def _attach_neighbors(db: AsyncSession, results: list[dict]) -> None:
    """The chunk on each side, from the same document and so the same access decision, within a token budget."""
    left = CONTEXT_TOKENS
    for r in results[:3]:
        rows = (await db.execute(select(Chunk).where(Chunk.document_id == uuid.UUID(r["document_id"]), Chunk.ord.in_((r["ord"] - 1, r["ord"] + 1))).order_by(Chunk.ord))).scalars().all()
        for n in rows:
            cost = n.token_estimate or len(n.text) // 4
            if cost <= left and str(n.id) not in {x["chunk_id"] for x in results}:
                r.setdefault("context", []).append({"lines": [n.start_line, n.end_line], "text": n.text})
                left -= cost


def _flag_conflicts(results: list[dict]) -> None:
    """Two documents with one title in one scope may disagree. Marked for a reader to judge; MOKS does not decide which is right."""
    groups: dict[tuple, list[dict]] = {}
    for r in results:
        groups.setdefault((r["title"].lower(), r["scope"]["environment"], r["scope"]["namespace"]), []).append(r)
    for rows in groups.values():
        if len({r["document_id"] for r in rows}) > 1:
            for r in rows:
                r["freshness"]["possible_conflict"] = True


def _line_url(url: str | None, start: int | None, end: int | None) -> str | None:
    if not url or "github" not in url or not start:
        return url
    return f"{url}#L{start}" + (f"-L{end}" if end and end != start else "")


async def rescope_source(db: AsyncSession, src: KnowledgeSource, old: tuple[str, str]) -> int:
    """After a source's scope changes, its documents must follow, or access filters would keep using
    the old scope. Documents given their own scope at upload keep it. The index is updated too, since
    it filters before PostgreSQL's authoritative check runs."""
    docs = (await db.execute(select(Document).where(Document.source_id == src.id))).scalars().all()
    moved = []
    for d in docs:
        env, ns = d.environment, d.namespace
        if env == old[0]:
            d.environment = src.scope_environment
        if ns == old[1]:
            d.namespace = src.scope_namespace
        if (env, ns) != (d.environment, d.namespace):
            moved.append(d)
    await db.flush()
    for d in moved:
        await _record_change(db, d, "upsert" if d.status == "active" else "delete")
    return len(moved)


async def stats(db: AsyncSession) -> dict:
    counts = dict((await db.execute(text("SELECT status, count(*) FROM documents GROUP BY status"))).all())
    chunks = (await db.execute(text("SELECT count(*) FROM chunks"))).scalar_one()
    idx = await active_index(db)
    cfg = await embedding_config(db)
    building = (await db.execute(select(EmbeddingIndex).where(EmbeddingIndex.backend == backend_name(), EmbeddingIndex.status == "building"))).scalars().first()
    outbox = dict((await db.execute(text("SELECT CASE WHEN status = 'pending' AND attempts > 0 THEN 'retrying' ELSE status END, count(*) FROM index_outbox WHERE status <> 'done' GROUP BY 1"))).all())
    stale = (await db.execute(select(func.count()).select_from(Document).where(Document.content_revision != Document.indexed_revision))).scalar_one()
    vectors = dict((await db.execute(select(Document.vector_state, func.count()).where(Document.status == "active").group_by(Document.vector_state))).all())
    old_pipeline = (await db.execute(select(func.count()).select_from(Document).where(Document.status == "active", Document.pipeline_version != PIPELINE_VERSION))).scalar_one()
    need = sum(vectors.get(k, 0) for k in ("complete", "missing", "unknown"))
    return {"backend": backend_name(), "documents": counts, "chunks": chunks, "pipeline": {"version": PIPELINE_VERSION, "documents_behind": old_pipeline},
            "outbox": {"pending": outbox.get("pending", 0), "retrying": outbox.get("retrying", 0), "dead": outbox.get("dead", 0), "documents_behind": stale},
            "embedding": {"complete": vectors.get("complete", 0), "missing": vectors.get("missing", 0), "unknown": vectors.get("unknown", 0), "not_applicable": vectors.get("not_applicable", 0),
                          "coverage": round(vectors.get("complete", 0) / need, 4) if need else None},
            "index": {"name": idx.index_name, "model": idx.model_key, "dimensions": idx.dimensions, "mode": "hybrid" if idx.dimensions else "keyword-only",
                      "note": idx.error, "reindex_needed": bool(cfg and idx.model_key != model_key(cfg))} if idx else None,
            "reindex_running": building is not None, "reindex_progress": {"chunks": building.chunk_count, "of": chunks} if building else None}


async def mark_indexed_now(db: AsyncSession, source: KnowledgeSource, status: str, error: str | None = None) -> None:
    await db.execute(update(KnowledgeSource).where(KnowledgeSource.id == source.id).values(
        last_sync_at=datetime.now(timezone.utc), last_sync_status=status, last_error=error))
