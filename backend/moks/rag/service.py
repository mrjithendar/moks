"""Knowledge retrieval: indexing, versioned embedding indexes, hybrid search with access control."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..connectors.base import ConnectorError
from ..db import session_scope
from ..llm.providers import LLMError, provider_for
from ..models import Chunk, Document, EmbeddingIndex, KnowledgeSource, LLMConfig
from ..observability import RETRIEVAL_LATENCY
from ..rbac import Principal
from . import access
from .backends import OpenSearchBackend, PineconeBackend, PostgresBackend, rrf
from .chunking import chunk, kind_of, title_of
from .secrets import clean, exclusion_reason

log = logging.getLogger("moks.rag")
EMBED_BATCH = 32
EMBED_BACKOFF_SECONDS = 5
REINDEX_EMBED_TRIES = 7  # a reindex is the one place worth waiting out a provider's rate limit
INDEX_LOCK = 7_203_114  # advisory lock: creating the first index
BUILD_STALE = timedelta(minutes=30)
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
    vec = (await provider_for(cfg).embed(["dimension probe"]))[0]
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
    if external():
        await external().ensure_index(idx.index_name, idx.dimensions)
    return idx


async def _embed_batch(provider, texts: list[str], tries: int) -> list[list[float]]:
    for attempt in range(tries):
        try:
            return await provider.embed(texts)
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
        out.extend(await _embed_batch(provider, texts[i:i + EMBED_BATCH], tries))
    if any(len(v) != idx.dimensions for v in out):
        raise LLMError("bad_response", f"embedding size differs from the index's {idx.dimensions} dimensions; reindex after a model change")
    return out


def _row(c: Chunk, d: Document, src: KnowledgeSource) -> dict:
    return {"chunk_id": str(c.id), "document_id": str(d.id), "environment": d.environment, "namespace": d.namespace,
            "repository": (src.config or {}).get("repository") or src.name, "service": d.service or "", "path": d.path,
            "heading": c.heading, "text": c.text}


async def index_document(db: AsyncSession, src: KnowledgeSource, path: str, raw: str, *, revision: str | None,
                         url: str | None, content_hash: str | None = None, extra: dict | None = None) -> Document:
    """Create or replace one document and its chunks. Excluded content is recorded without text."""
    extra = extra or {}
    content_hash = content_hash or hashlib.sha256(raw.encode()).hexdigest()
    doc = (await db.execute(select(Document).where(Document.source_id == src.id, Document.path == path))).scalar_one_or_none()
    if doc and doc.content_hash == content_hash and doc.status == "active":
        return doc  # unchanged: incremental sync skips it
    if doc is None:
        doc = Document(source_id=src.id, path=path, content_hash=content_hash)
        db.add(doc)
    await _drop_chunks(db, doc)
    doc.content_hash, doc.revision, doc.url = content_hash, revision, url
    doc.kind = kind_of(path)
    doc.environment = extra.get("environment", src.scope_environment)
    doc.namespace = extra.get("namespace", src.scope_namespace)
    doc.service, doc.owner = extra.get("service", src.service), extra.get("owner", src.owner)
    doc.size_bytes = len(raw.encode())
    doc.updated_at = datetime.now(timezone.utc)
    reason = exclusion_reason(path, raw)
    if reason:
        doc.status, doc.excluded_reason, doc.title = "excluded", reason, path.rsplit("/", 1)[-1]
        await db.flush()
        return doc
    text_clean = clean(raw)
    doc.status, doc.excluded_reason, doc.title = "active", None, extra.get("title") or title_of(path, text_clean)
    await db.flush()
    pieces = chunk(path, text_clean)
    chunks = [Chunk(document_id=doc.id, ord=i, heading=p.heading[:480], text=p.text, start_line=p.start_line, end_line=p.end_line)
              for i, p in enumerate(pieces)]
    db.add_all(chunks)
    await db.flush()
    idx = await ensure_index(db)
    rows = [_row(c, doc, src) for c in chunks]
    embeddings = None
    try:
        embeddings = await _embed(db, idx, [f"{c.heading}\n{c.text}" for c in chunks]) if chunks else None
    except LLMError as exc:
        log.warning("embedding failed; indexed keyword-only", extra={"event": "embedding_failed", "errorType": exc.kind})
    if external():
        await external().index_chunks(idx.index_name, rows, embeddings)
    else:
        await PostgresBackend(db).index_chunks(idx, rows, embeddings)
    doc.indexed_at = datetime.now(timezone.utc)
    return doc


async def _drop_chunks(db: AsyncSession, doc: Document) -> None:
    if doc.id is None:
        return
    if external():
        idx = await active_index(db)
        if idx:
            await external().delete_document(idx.index_name, doc.id)
    await db.execute(delete(Chunk).where(Chunk.document_id == doc.id))


async def remove_document(db: AsyncSession, doc: Document) -> None:
    """Deletion propagation: the document disappears from search immediately."""
    await _drop_chunks(db, doc)
    doc.status, doc.updated_at = "deleted", datetime.now(timezone.utc)


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


async def reindex(db: AsyncSession) -> dict:
    """Build a new index version beside the active one, then switch in one committed transaction.
    Search uses the old index until that commit. A failed build leaves the old index active. The
    old physical index is deleted only after the switch has committed (cleanup_retired)."""
    cfg = await embedding_config(db)
    if backend_name() == "postgres" and cfg and not await PostgresBackend(db).has_vector():
        raise ConnectorError("config", "pgvector is not installed in this PostgreSQL; use the opensearch backend or a pgvector image")
    await db.execute(update(EmbeddingIndex).where(
        EmbeddingIndex.backend == backend_name(), EmbeddingIndex.status == "building",
        EmbeddingIndex.heartbeat_at < datetime.now(timezone.utc) - BUILD_STALE).values(status="failed", error="build stopped responding", cleanup_pending=True))
    await db.commit()
    dims = await embedding_dimensions(cfg) if cfg else None
    new = EmbeddingIndex(backend=backend_name(), llm_config_id=cfg.id if cfg else None, model_key=model_key(cfg),
                         dimensions=dims, index_name="", status="building", heartbeat_at=datetime.now(timezone.utc))
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
                new.heartbeat_at = datetime.now(timezone.utc)
                await db.commit()
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


async def search(db: AsyncSession, principal: Principal, query: str, k: int = 8, environment: str | None = None,
                 namespace: str | None = None) -> dict:
    start = time.monotonic()
    envs, nss = access.prefilter(principal)
    if envs == [] and nss == []:
        return {"results": [], "mode": "none", "note": "you have no knowledge access"}
    if environment:
        envs = [environment] if envs is None or environment in envs else []
    pool = max(k * 4, 20)
    idx = await active_index(db)
    mode = "keyword"
    if backend_name() == "opensearch":
        if idx is None:
            return {"results": [], "mode": "empty", "note": "nothing has been indexed yet"}
        rankings = [await opensearch().keyword(idx.index_name, query, envs, nss, pool)]
    else:
        rankings = [await PostgresBackend(db).keyword(query, envs, nss, pool)]
    cfg = await embedding_config(db)
    if idx and idx.dimensions and cfg and idx.llm_config_id == cfg.id and idx.model_key == model_key(cfg):
        try:
            qv = (await provider_for(cfg).embed([query]))[0]
            if external():
                rankings.append(await external().vector(idx.index_name, qv, envs, nss, pool))
            elif await PostgresBackend(db).has_vector():
                rankings.append(await PostgresBackend(db).vector(qv, idx.id, envs, nss, pool))
            mode = "hybrid"
        except (LLMError, ConnectorError) as exc:
            log.warning("vector search unavailable", extra={"event": "vector_search_failed", "errorType": type(exc).__name__})
    fused = rrf(*rankings)
    ids = [uuid.UUID(cid) for cid, _ in fused[: pool]]
    if not ids:
        RETRIEVAL_LATENCY.labels(backend_name()).observe(time.monotonic() - start)
        return {"results": [], "mode": mode}
    rows = (await db.execute(select(Chunk, Document, KnowledgeSource).join(Document, Document.id == Chunk.document_id)
                             .join(KnowledgeSource, KnowledgeSource.id == Document.source_id)
                             .where(Chunk.id.in_(ids), Document.status == "active"))).all()
    by_id = {str(c.id): (c, d, s) for c, d, s in rows}
    results: list[dict[str, Any]] = []
    for cid, score in fused:
        if cid not in by_id:
            continue
        c, d, s = by_id[cid]
        # Authoritative post-filter: never return what the principal may not read.
        if not access.can_read(principal, d.environment, d.namespace):
            continue
        if namespace and d.namespace not in ("*", namespace):
            continue
        results.append({
            "chunk_id": cid, "score": round(score, 5), "title": d.title, "heading": c.heading, "text": c.text,
            "citation": {"source": s.name, "source_type": s.type, "repository": (s.config or {}).get("repository"),
                         "path": d.path, "revision": d.revision, "lines": [c.start_line, c.end_line],
                         "url": _line_url(d.url, c.start_line, c.end_line)},
            "scope": {"environment": d.environment, "namespace": d.namespace, "service": d.service, "owner": d.owner},
            "freshness": {"document_updated": d.updated_at.isoformat() if d.updated_at else None,
                          "source_synced": s.last_sync_at.isoformat() if s.last_sync_at else None},
        })
        if len(results) >= k:
            break
    RETRIEVAL_LATENCY.labels(backend_name()).observe(time.monotonic() - start)
    return {"results": results, "mode": mode, "index": idx.index_name if idx else None}


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
    idx = await active_index(db)
    if moved and idx is not None and external():
        for d in moved:
            await external().update_scope(idx.index_name, d.id, d.environment, d.namespace)
    return len(moved)


async def stats(db: AsyncSession) -> dict:
    counts = dict((await db.execute(text("SELECT status, count(*) FROM documents GROUP BY status"))).all())
    chunks = (await db.execute(text("SELECT count(*) FROM chunks"))).scalar_one()
    idx = await active_index(db)
    cfg = await embedding_config(db)
    building = (await db.execute(select(EmbeddingIndex).where(EmbeddingIndex.backend == backend_name(), EmbeddingIndex.status == "building"))).scalars().first()
    return {"backend": backend_name(), "documents": counts, "chunks": chunks,
            "index": {"name": idx.index_name, "model": idx.model_key, "dimensions": idx.dimensions, "mode": "hybrid" if idx.dimensions else "keyword-only",
                      "note": idx.error, "reindex_needed": bool(cfg and idx.model_key != model_key(cfg))} if idx else None,
            "reindex_running": building is not None}


async def mark_indexed_now(db: AsyncSession, source: KnowledgeSource, status: str, error: str | None = None) -> None:
    await db.execute(update(KnowledgeSource).where(KnowledgeSource.id == source.id).values(
        last_sync_at=datetime.now(timezone.utc), last_sync_status=status, last_error=error))
