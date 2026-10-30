"""Knowledge retrieval: indexing, versioned embedding indexes, hybrid search with access control."""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..connectors.base import ConnectorError
from ..llm.providers import LLMError, provider_for
from ..models import Chunk, Document, EmbeddingIndex, KnowledgeSource, LLMConfig
from ..observability import RETRIEVAL_LATENCY
from ..rbac import Principal
from . import access
from .backends import OpenSearchBackend, PostgresBackend, rrf
from .chunking import chunk, kind_of, title_of
from .secrets import clean, exclusion_reason

log = logging.getLogger("moks.rag")
EMBED_BATCH = 32
_opensearch: OpenSearchBackend | None = None


def opensearch() -> OpenSearchBackend:
    global _opensearch
    if _opensearch is None:
        s = get_settings()
        _opensearch = OpenSearchBackend(s.opensearch_url, s.opensearch_username, s.opensearch_password, s.opensearch_verify_tls)
    return _opensearch


def backend_name() -> str:
    return get_settings().rag_backend


async def embedding_config(db: AsyncSession) -> LLMConfig | None:
    return (await db.execute(select(LLMConfig).where(LLMConfig.purpose == "embedding", LLMConfig.is_active.is_(True)))).scalar_one_or_none()


def model_key(cfg: LLMConfig | None) -> str:
    return f"{cfg.provider}/{cfg.model}/{cfg.embedding_dimensions or 'default'}" if cfg else "keyword-only"


async def active_index(db: AsyncSession) -> EmbeddingIndex | None:
    return (await db.execute(select(EmbeddingIndex).where(EmbeddingIndex.backend == backend_name(),
                                                          EmbeddingIndex.status == "active"))).scalar_one_or_none()


async def ensure_index(db: AsyncSession) -> EmbeddingIndex:
    """The active index for the current backend; created (keyword-only or for the active embedding
    model) when none exists. A model change is handled by reindex(), not here."""
    idx = await active_index(db)
    if idx:
        return idx
    cfg = await embedding_config(db)
    if backend_name() == "postgres" and cfg and not await PostgresBackend(db).has_vector():
        cfg = None  # pgvector not installed: keyword-only
    idx = EmbeddingIndex(backend=backend_name(), llm_config_id=cfg.id if cfg else None, model_key=model_key(cfg),
                         dimensions=cfg.embedding_dimensions if cfg else None, index_name="", status="active",
                         activated_at=datetime.now(timezone.utc))
    db.add(idx)
    await db.flush()
    idx.index_name = f"moks-chunks-{str(idx.id)[:8]}"
    if backend_name() == "opensearch":
        await opensearch().ensure_index(idx.index_name, idx.dimensions)
    return idx


async def _embed(db: AsyncSession, idx: EmbeddingIndex, texts: list[str]) -> list[list[float]] | None:
    if not idx.llm_config_id:
        return None
    cfg = await db.get(LLMConfig, idx.llm_config_id)
    if cfg is None:
        return None
    provider = provider_for(cfg)
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        out.extend(await provider.embed(texts[i:i + EMBED_BATCH]))
    if idx.dimensions is None and out:
        idx.dimensions = len(out[0])
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
    if backend_name() == "opensearch":
        await opensearch().index_chunks(idx.index_name, rows, embeddings)
    else:
        await PostgresBackend(db).index_chunks(idx, rows, embeddings)
    doc.indexed_at = datetime.now(timezone.utc)
    return doc


async def _drop_chunks(db: AsyncSession, doc: Document) -> None:
    if doc.id is None:
        return
    if backend_name() == "opensearch":
        idx = await active_index(db)
        if idx:
            await opensearch().delete_document(idx.index_name, doc.id)
    await db.execute(delete(Chunk).where(Chunk.document_id == doc.id))


async def remove_document(db: AsyncSession, doc: Document) -> None:
    """Deletion propagation: the document disappears from search immediately."""
    await _drop_chunks(db, doc)
    doc.status, doc.updated_at = "deleted", datetime.now(timezone.utc)


async def reindex(db: AsyncSession) -> dict:
    """Build a new index version for the active embedding model from the stored chunks, then switch
    to it and retire the old one. Search keeps using the old index until the switch."""
    cfg = await embedding_config(db)
    if backend_name() == "postgres" and cfg and not await PostgresBackend(db).has_vector():
        raise ConnectorError("config", "pgvector is not installed in this PostgreSQL; use the opensearch backend or a pgvector image")
    old = await active_index(db)
    new = EmbeddingIndex(backend=backend_name(), llm_config_id=cfg.id if cfg else None, model_key=model_key(cfg),
                         dimensions=cfg.embedding_dimensions if cfg else None, index_name="", status="building")
    db.add(new)
    await db.flush()
    new.index_name = f"moks-chunks-{str(new.id)[:8]}"
    rows_q = (await db.execute(select(Chunk, Document, KnowledgeSource).join(Document, Document.id == Chunk.document_id)
                               .join(KnowledgeSource, KnowledgeSource.id == Document.source_id)
                               .where(Document.status == "active").order_by(Chunk.id))).all()
    first = True
    total = 0
    for i in range(0, len(rows_q), 200):
        batch = rows_q[i:i + 200]
        rows = [_row(c, d, s) for c, d, s in batch]
        embs = await _embed(db, new, [f"{r['heading']}\n{r['text']}" for r in rows])
        if backend_name() == "opensearch":
            if first:
                await opensearch().ensure_index(new.index_name, new.dimensions)
            await opensearch().index_chunks(new.index_name, rows, embs)
        else:
            await PostgresBackend(db).index_chunks(new, rows, embs)
        first = False
        total += len(rows)
    if backend_name() == "opensearch" and first:
        await opensearch().ensure_index(new.index_name, new.dimensions)
    if old:
        old.status = "retired"
        await db.flush()
    new.status, new.activated_at, new.chunk_count = "active", datetime.now(timezone.utc), total
    await db.flush()
    if old and backend_name() == "opensearch":
        await opensearch().drop_index(old.index_name)
    elif old:
        await db.execute(text("DELETE FROM chunk_embeddings WHERE index_id = :i"), {"i": old.id})
    return {"index": new.index_name, "model": new.model_key, "chunks": total, "replaced": old.index_name if old else None}


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
    if idx and cfg and idx.llm_config_id == cfg.id and idx.model_key == model_key(cfg):
        try:
            qv = (await provider_for(cfg).embed([query]))[0]
            if backend_name() == "opensearch":
                rankings.append(await opensearch().vector(idx.index_name, qv, envs, nss, pool))
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


async def stats(db: AsyncSession) -> dict:
    counts = dict((await db.execute(text("SELECT status, count(*) FROM documents GROUP BY status"))).all())
    chunks = (await db.execute(text("SELECT count(*) FROM chunks"))).scalar_one()
    idx = await active_index(db)
    return {"backend": backend_name(), "documents": counts, "chunks": chunks,
            "index": {"name": idx.index_name, "model": idx.model_key, "dimensions": idx.dimensions} if idx else None}


async def mark_indexed_now(db: AsyncSession, source: KnowledgeSource, status: str, error: str | None = None) -> None:
    await db.execute(update(KnowledgeSource).where(KnowledgeSource.id == source.id).values(
        last_sync_at=datetime.now(timezone.utc), last_sync_status=status, last_error=error))
