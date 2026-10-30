"""Retrieval backends. PostgreSQL is always the source of truth for documents and chunks; a
backend only indexes and searches them.

postgres   - full-text (websearch_to_tsquery, ts_rank_cd) and, when the pgvector extension is
             installed and an embedding index is active, cosine k-NN; fused with RRF.
opensearch - one index per embedding version (moks-chunks-<version>), BM25 multi_match plus
             knn_vector; fused client-side with RRF so no search pipeline plugin is required.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..connectors.base import ConnectorError

RRF_K = 60


@dataclass
class Hit:
    chunk_id: str
    score: float
    sources: list[str] = field(default_factory=list)


def rrf(*rankings: list[str]) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def _filters_sql(envs: list[str] | None, nss: list[str] | None, params: dict) -> str:
    clauses = ["d.status = 'active'"]
    if envs is not None:
        clauses.append("(d.environment = '*' OR d.environment = ANY(:envs))")
        params["envs"] = envs
    if nss is not None:
        clauses.append("(d.namespace = '*' OR d.namespace = ANY(:nss))")
        params["nss"] = nss
    return " AND ".join(clauses)


class PostgresBackend:
    name = "postgres"

    def __init__(self, db: AsyncSession):
        self.db = db

    async def has_vector(self) -> bool:
        row = (await self.db.execute(text(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'chunk_embeddings' AND column_name = 'embedding'"))).first()
        return row is not None

    async def keyword(self, query: str, envs, nss, k: int) -> list[str]:
        params: dict[str, Any] = {"q": query, "k": k}
        where = _filters_sql(envs, nss, params)
        rows = (await self.db.execute(text(f"""
            SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE {where} AND c.tsv @@ websearch_to_tsquery('english', :q)
            ORDER BY ts_rank_cd(c.tsv, websearch_to_tsquery('english', :q)) DESC LIMIT :k"""), params)).all()
        if not rows:  # fall back to OR-semantics so partial matches still rank
            terms = [t for t in "".join(ch if ch.isalnum() else " " for ch in query).split() if len(t) > 2][:12]
            if terms:
                params["q2"] = " | ".join(terms)
                rows = (await self.db.execute(text(f"""
                    SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id
                    WHERE {where} AND c.tsv @@ to_tsquery('english', :q2)
                    ORDER BY ts_rank_cd(c.tsv, to_tsquery('english', :q2)) DESC LIMIT :k"""), params)).all()
        return [str(r[0]) for r in rows]

    async def vector(self, embedding: list[float], index_id: uuid.UUID, envs, nss, k: int) -> list[str]:
        params: dict[str, Any] = {"v": json.dumps(embedding), "k": k, "idx": index_id}
        where = _filters_sql(envs, nss, params)
        rows = (await self.db.execute(text(f"""
            SELECT c.id FROM chunk_embeddings e JOIN chunks c ON c.id = e.chunk_id JOIN documents d ON d.id = c.document_id
            WHERE {where} AND e.index_id = :idx
            ORDER BY e.embedding <=> CAST(:v AS vector) LIMIT :k"""), params)).all()
        return [str(r[0]) for r in rows]

    async def upsert_embeddings(self, index_id: uuid.UUID, items: list[tuple[uuid.UUID, list[float]]]) -> None:
        for chunk_id, emb in items:
            await self.db.execute(text("""
                INSERT INTO chunk_embeddings (chunk_id, index_id, embedding) VALUES (:c, :i, CAST(:v AS vector))
                ON CONFLICT (chunk_id, index_id) DO UPDATE SET embedding = EXCLUDED.embedding"""),
                {"c": chunk_id, "i": index_id, "v": json.dumps(emb)})

    async def delete_document(self, document_id: uuid.UUID) -> None:
        pass  # chunk rows (and their embeddings) are deleted by FK cascade

    async def index_chunks(self, index, rows: list[dict], embeddings: list[list[float]] | None) -> None:
        if embeddings and index is not None:
            await self.upsert_embeddings(index.id, [(uuid.UUID(r["chunk_id"]), e) for r, e in zip(rows, embeddings)])


class OpenSearchBackend:
    name = "opensearch"

    def __init__(self, url: str, username: str = "", password: str = "", verify: bool = True,
                 transport: httpx.AsyncBaseTransport | None = None):
        if not url:
            raise ConnectorError("config", "MOKS_OPENSEARCH_URL is required for the opensearch backend")
        auth = (username, password) if username else None
        self.client = httpx.AsyncClient(base_url=url.rstrip("/"), auth=auth, verify=verify, timeout=20, transport=transport)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _req(self, method: str, path: str, body: Any = None, ok404: bool = False, ndjson: str | None = None) -> dict:
        try:
            if ndjson is not None:
                r = await self.client.request(method, path, content=ndjson, headers={"Content-Type": "application/x-ndjson"})
            else:
                r = await self.client.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise ConnectorError("unavailable", f"cannot reach OpenSearch ({type(exc).__name__})", retryable=True) from None
        if r.status_code == 404 and ok404:
            return {}
        if r.status_code >= 400:
            raise ConnectorError("unavailable" if r.status_code >= 500 else "bad_request", f"OpenSearch {method} {path}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}

    async def health(self) -> dict:
        return await self._req("GET", "/_cluster/health")

    async def ensure_index(self, name: str, dims: int | None) -> None:
        if (await self.client.head(f"/{name}")).status_code == 200:
            return
        props: dict[str, Any] = {
            "chunk_id": {"type": "keyword"}, "document_id": {"type": "keyword"}, "environment": {"type": "keyword"},
            "namespace": {"type": "keyword"}, "repository": {"type": "keyword"}, "service": {"type": "keyword"},
            "path": {"type": "text", "fields": {"raw": {"type": "keyword"}}}, "heading": {"type": "text"}, "text": {"type": "text"},
        }
        settings: dict[str, Any] = {"index": {"number_of_shards": 1, "number_of_replicas": 0}}
        if dims:
            props["embedding"] = {"type": "knn_vector", "dimension": dims,
                                  "method": {"name": "hnsw", "engine": "lucene", "space_type": "cosinesimil"}}
            settings["index"]["knn"] = True
        await self._req("PUT", f"/{name}", {"settings": settings, "mappings": {"properties": props}})

    async def index_chunks(self, index_name: str, rows: list[dict], embeddings: list[list[float]] | None) -> None:
        lines = []
        for i, r in enumerate(rows):
            doc = {k: r[k] for k in ("chunk_id", "document_id", "environment", "namespace", "repository", "service", "path", "heading", "text")}
            if embeddings:
                doc["embedding"] = embeddings[i]
            lines.append(json.dumps({"index": {"_index": index_name, "_id": r["chunk_id"]}}))
            lines.append(json.dumps(doc))
        if lines:
            resp = await self._req("POST", "/_bulk?refresh=wait_for", ndjson="\n".join(lines) + "\n")
            if resp.get("errors"):
                first = next((it for it in resp.get("items", []) if (it.get("index") or {}).get("error")), {})
                raise ConnectorError("bad_request", f"OpenSearch bulk indexing failed: {json.dumps(first)[:300]}")

    async def delete_document(self, index_name: str, document_id: uuid.UUID) -> None:
        await self._req("POST", f"/{index_name}/_delete_by_query?refresh=true",
                        {"query": {"term": {"document_id": str(document_id)}}}, ok404=True)

    def _filter(self, envs, nss) -> list[dict]:
        f: list[dict] = []
        if envs is not None:
            f.append({"terms": {"environment": ["*", *envs]}})
        if nss is not None:
            f.append({"terms": {"namespace": ["*", *nss]}})
        return f

    async def keyword(self, index_name: str, query: str, envs, nss, k: int) -> list[str]:
        body = {"size": k, "_source": ["chunk_id"], "query": {"bool": {
            "must": [{"multi_match": {"query": query, "fields": ["heading^2", "text", "path^1.5"]}}], "filter": self._filter(envs, nss)}}}
        resp = await self._req("POST", f"/{index_name}/_search", body, ok404=True)
        return [h["_source"]["chunk_id"] for h in (resp.get("hits") or {}).get("hits", [])]

    async def vector(self, index_name: str, embedding: list[float], envs, nss, k: int) -> list[str]:
        body = {"size": k, "_source": ["chunk_id"], "query": {"knn": {"embedding": {
            "vector": embedding, "k": k, "filter": {"bool": {"filter": self._filter(envs, nss)}} if (envs is not None or nss is not None) else None}}}}
        if body["query"]["knn"]["embedding"]["filter"] is None:
            del body["query"]["knn"]["embedding"]["filter"]
        resp = await self._req("POST", f"/{index_name}/_search", body, ok404=True)
        return [h["_source"]["chunk_id"] for h in (resp.get("hits") or {}).get("hits", [])]

    async def drop_index(self, index_name: str) -> None:
        await self._req("DELETE", f"/{index_name}", ok404=True)
