"""Retrieval backends. PostgreSQL is always the source of truth for documents and chunks; a
backend only indexes and searches them.

postgres   - full-text (websearch_to_tsquery, ts_rank_cd) and, when the pgvector extension is
             installed and an embedding index is active, cosine k-NN; fused with RRF.
opensearch - one index per embedding version (moks-chunks-<version>), BM25 multi_match plus
             knn_vector; fused client-side with RRF so no search pipeline plugin is required.
             Self-hosted (basic auth) or an Amazon OpenSearch Service domain (SigV4).
pinecone   - vectors only, one index per embedding version. Keyword search stays in PostgreSQL, which
             holds the text. Pinecone receives vectors, document ids and scope names, never chunk text.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..connectors.base import ConnectorError
from . import access

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


def _filters_sql(found: list[access.Grant], params: dict) -> str:
    return f"d.status = 'active' AND s.lifecycle = 'active' AND {access.sql(found, params)}"


class PostgresBackend:
    name = "postgres"

    def __init__(self, db: AsyncSession):
        self.db = db

    async def has_vector(self) -> bool:
        row = (await self.db.execute(text(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'chunk_embeddings' AND column_name = 'embedding'"))).first()
        return row is not None

    async def keyword(self, query: str, found: list[access.Grant], k: int) -> list[str]:
        params: dict[str, Any] = {"q": query, "k": k}
        where = _filters_sql(found, params)
        rows = (await self.db.execute(text(f"""
            SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id JOIN knowledge_sources s ON s.id = d.source_id
            WHERE {where} AND c.tsv @@ websearch_to_tsquery('english', :q)
            ORDER BY ts_rank_cd(c.tsv, websearch_to_tsquery('english', :q)) DESC LIMIT :k"""), params)).all()
        if not rows:  # fall back to OR-semantics so partial matches still rank
            terms = [t for t in "".join(ch if ch.isalnum() else " " for ch in query).split() if len(t) > 2][:12]
            if terms:
                params["q2"] = " | ".join(terms)
                rows = (await self.db.execute(text(f"""
                    SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id JOIN knowledge_sources s ON s.id = d.source_id
                    WHERE {where} AND c.tsv @@ to_tsquery('english', :q2)
                    ORDER BY ts_rank_cd(c.tsv, to_tsquery('english', :q2)) DESC LIMIT :k"""), params)).all()
        return [str(r[0]) for r in rows]

    async def identifiers(self, tokens: list[str], found: list[access.Grant], k: int) -> list[str]:
        """Exact matches for error codes, resource names, paths and configuration keys, which a text
        search stems and splits. PostgreSQL holds the text whichever backend ranks the rest."""
        if not tokens:
            return []
        params: dict[str, Any] = {"k": k}
        where = _filters_sql(found, params)
        hits = []
        for i, t in enumerate(tokens[:6]):
            params[f"t{i}"] = "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            hits.append(f"(CASE WHEN c.text ILIKE :t{i} OR d.path ILIKE :t{i} OR c.heading ILIKE :t{i} THEN 1 ELSE 0 END)")
        score = " + ".join(hits)
        rows = (await self.db.execute(text(f"""
            SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id JOIN knowledge_sources s ON s.id = d.source_id
            WHERE {where} AND ({score}) > 0 ORDER BY ({score}) DESC, c.id LIMIT :k"""), params)).all()
        return [str(r[0]) for r in rows]

    async def vector(self, embedding: list[float], index_id: uuid.UUID, found: list[access.Grant], k: int) -> list[str]:
        params: dict[str, Any] = {"v": json.dumps(embedding), "k": k, "idx": index_id}
        where = _filters_sql(found, params)
        rows = (await self.db.execute(text(f"""
            SELECT c.id FROM chunk_embeddings e JOIN chunks c ON c.id = e.chunk_id JOIN documents d ON d.id = c.document_id JOIN knowledge_sources s ON s.id = d.source_id
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


class SigV4(httpx.Auth):
    """Signs each request for an Amazon OpenSearch Service domain. Credentials come from the AWS
    default chain, read per request so that rotated or short-lived ones are picked up."""
    SERVICE = "es"

    def __init__(self, region: str):
        self.region = region

    def _credentials(self):
        import botocore.session
        from botocore.exceptions import BotoCoreError

        try:
            found = botocore.session.get_session().get_credentials()
            frozen = found.get_frozen_credentials() if found else None
        except BotoCoreError as exc:
            raise ConnectorError("unavailable", f"cannot read AWS credentials ({type(exc).__name__})", retryable=True) from None
        if frozen is None or not frozen.access_key:
            raise ConnectorError("config", "no AWS credentials available to MOKS for OpenSearch")
        return frozen

    def sign(self, request: httpx.Request, credentials) -> None:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        signed = {"Host": request.url.netloc.decode()}
        if "Content-Type" in request.headers:
            signed["Content-Type"] = request.headers["Content-Type"]
        aws = AWSRequest(method=request.method, url=str(request.url), data=request.content, headers=signed)
        SigV4Auth(credentials, self.SERVICE, self.region).add_auth(aws)
        for header in ("Authorization", "X-Amz-Date", "X-Amz-Security-Token"):
            if header in aws.headers:
                request.headers[header] = aws.headers[header]

    async def async_auth_flow(self, request: httpx.Request):
        await request.aread()
        self.sign(request, await asyncio.to_thread(self._credentials))
        yield request


class OpenSearchBackend:
    name = "opensearch"

    def __init__(self, url: str, username: str = "", password: str = "", verify: bool = True,
                 transport: httpx.AsyncBaseTransport | None = None, aws_region: str = ""):
        if not url:
            raise ConnectorError("config", "MOKS_OPENSEARCH_URL is required for the opensearch backend")
        if aws_region and username:
            raise ConnectorError("config", "set MOKS_OPENSEARCH_AWS_REGION or MOKS_OPENSEARCH_USERNAME, not both")
        if aws_region and not url.startswith("https://"):
            raise ConnectorError("config", "an Amazon OpenSearch Service endpoint must use https")
        auth: Any = SigV4(aws_region) if aws_region else (username, password) if username else None
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

    async def vector_dimensions(self, name: str) -> int | None:
        """Dimension of an existing index's vector field; None when it is keyword-only or absent."""
        resp = await self._req("GET", f"/{name}/_mapping", ok404=True)
        props = ((resp.get(name) or {}).get("mappings") or {}).get("properties") or {}
        return (props.get("embedding") or {}).get("dimension")

    async def ensure_index(self, name: str, dims: int | None) -> None:
        if (await self.client.head(f"/{name}")).status_code == 200:
            found = await self.vector_dimensions(name)
            if found != dims:
                raise ConnectorError("config", f"OpenSearch index {name} has vector dimension {found}, expected {dims}; run a reindex")
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

    async def update_scope(self, index_name: str, document_id: uuid.UUID, environment: str, namespace: str) -> None:
        await self._req("POST", f"/{index_name}/_update_by_query?refresh=true&conflicts=proceed", {
            "query": {"term": {"document_id": str(document_id)}},
            "script": {"lang": "painless", "source": "ctx._source.environment = params.e; ctx._source.namespace = params.n",
                       "params": {"e": environment, "n": namespace}}}, ok404=True)

    async def count_document(self, index_name: str, document_id: uuid.UUID) -> int | None:
        resp = await self._req("POST", f"/{index_name}/_count", {"query": {"term": {"document_id": str(document_id)}}}, ok404=True)
        return resp.get("count") if resp else 0

    async def keyword(self, index_name: str, query: str, found: list[access.Grant], k: int) -> tuple[list[str], bool]:
        flt, exact = access.opensearch(found)
        body = {"size": k, "_source": ["chunk_id"], "query": {"bool": {
            "must": [{"multi_match": {"query": query, "fields": ["heading^2", "text", "path^1.5"]}}], "filter": [flt] if flt else []}}}
        resp = await self._req("POST", f"/{index_name}/_search", body, ok404=True)
        return [h["_source"]["chunk_id"] for h in (resp.get("hits") or {}).get("hits", [])], exact

    async def vector(self, index_name: str, embedding: list[float], found: list[access.Grant], k: int) -> tuple[list[str], bool]:
        flt, exact = access.opensearch(found)
        knn: dict[str, Any] = {"vector": embedding, "k": k}
        if flt:
            knn["filter"] = flt  # applied during the search, so unreadable neighbours do not use up k
        resp = await self._req("POST", f"/{index_name}/_search", {"size": k, "_source": ["chunk_id"], "query": {"knn": {"embedding": knn}}}, ok404=True)
        return [h["_source"]["chunk_id"] for h in (resp.get("hits") or {}).get("hits", [])], exact

    async def drop_index(self, index_name: str) -> None:
        await self._req("DELETE", f"/{index_name}", ok404=True)


class PineconeBackend:
    """Pinecone REST API, pinned to one version. A derived copy: PostgreSQL can rebuild all of it."""

    name = "pinecone"
    API_VERSION = "2026-07"
    CONTROL = "https://api.pinecone.io"
    NAMESPACE = "moks"
    UPSERT_BATCH = 25  # high-dimension vectors serialize to tens of KB each
    READY_SECONDS = 120
    POLL_SECONDS = 2.0
    TRIES = 4

    def __init__(self, api_key: str | None, cloud: str = "aws", region: str = "us-east-1", transport: httpx.AsyncBaseTransport | None = None):
        if not api_key:
            raise ConnectorError("config", "MOKS_CRED_PINECONE_API_KEY is required for the pinecone backend")
        self.cloud, self.region = cloud, region
        self.client = httpx.AsyncClient(timeout=30, transport=transport, headers={"Api-Key": api_key, "X-Pinecone-Api-Version": self.API_VERSION})
        self._hosts: dict[str, str] = {}

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _req(self, method: str, url: str, body: Any = None, ok404: bool = False) -> dict:
        for attempt in range(self.TRIES):
            try:
                r = await self.client.request(method, url, json=body)
            except httpx.HTTPError as exc:
                raise ConnectorError("unavailable", f"cannot reach Pinecone ({type(exc).__name__})", retryable=True) from None
            if r.status_code != 429 or attempt == self.TRIES - 1:
                break
            await asyncio.sleep(2 ** attempt)
        if r.status_code == 404 and ok404:
            return {}
        if r.status_code in (401, 403):
            raise ConnectorError("auth", f"Pinecone rejected the credentials (HTTP {r.status_code})")
        if r.status_code >= 400:
            raise ConnectorError("unavailable" if r.status_code >= 500 or r.status_code == 429 else "bad_request", f"Pinecone {method}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}

    async def _describe(self, name: str) -> dict:
        return await self._req("GET", f"{self.CONTROL}/indexes/{name}", ok404=True)

    async def _host(self, name: str) -> str | None:
        if name not in self._hosts:
            info = await self._describe(name)
            if not info.get("host"):
                return None
            self._hosts[name] = info["host"]
        return self._hosts[name]

    async def health(self) -> dict:
        return await self._req("GET", f"{self.CONTROL}/indexes")

    async def ensure_index(self, name: str, dims: int | None) -> None:
        if not dims:
            return  # keyword-only: nothing to store here
        info = await self._describe(name)
        if not info:
            info = await self._req("POST", f"{self.CONTROL}/indexes", {
                "name": name, "deployment": {"deployment_type": "managed", "cloud": self.cloud, "region": self.region},
                # The reserved field _values makes a vector index. Any other name makes a document index,
                # which refuses the /vectors endpoints used here.
                "schema": {"fields": {"_values": {"type": "dense_vector", "dimension": dims, "metric": "cosine"}}}})
        found = (((info.get("schema") or {}).get("fields") or {}).get("_values") or {}).get("dimension", dims)
        if found != dims:
            raise ConnectorError("config", f"Pinecone index {name} has vector dimension {found}, expected {dims}; run a reindex")
        for _ in range(int(self.READY_SECONDS / self.POLL_SECONDS)):
            if (info.get("status") or {}).get("ready"):
                self._hosts[name] = info["host"]
                return
            await asyncio.sleep(self.POLL_SECONDS)
            info = await self._describe(name)
        raise ConnectorError("unavailable", f"Pinecone index {name} was not ready within {self.READY_SECONDS}s")

    async def index_chunks(self, index_name: str, rows: list[dict], embeddings: list[list[float]] | None) -> None:
        host = await self._host(index_name) if embeddings else None
        if not host:
            return
        vectors = [{"id": r["chunk_id"], "values": v, "metadata": {"document_id": r["document_id"], "environment": r["environment"], "namespace": r["namespace"]}}
                   for r, v in zip(rows, embeddings or [])]
        for i in range(0, len(vectors), self.UPSERT_BATCH):
            await self._req("POST", f"https://{host}/vectors/upsert", {"vectors": vectors[i:i + self.UPSERT_BATCH], "namespace": self.NAMESPACE})

    async def delete_document(self, index_name: str, document_id: uuid.UUID) -> None:
        host = await self._host(index_name)
        if host:
            await self._req("POST", f"https://{host}/vectors/delete", {"filter": {"document_id": {"$eq": str(document_id)}}, "namespace": self.NAMESPACE}, ok404=True)

    async def update_scope(self, index_name: str, document_id: uuid.UUID, environment: str, namespace: str) -> None:
        host = await self._host(index_name)
        if host:
            await self._req("POST", f"https://{host}/vectors/update", {"filter": {"document_id": {"$eq": str(document_id)}}, "namespace": self.NAMESPACE,
                                                                       "setMetadata": {"environment": environment, "namespace": namespace}}, ok404=True)

    async def count_document(self, index_name: str, document_id: uuid.UUID) -> int | None:
        return None  # Pinecone cannot count by metadata; revision tracking is what is available

    async def vector(self, index_name: str, embedding: list[float], found: list[access.Grant], k: int) -> tuple[list[str], bool]:
        host = await self._host(index_name)
        if not host:
            return [], True
        flt, exact = access.pinecone(found)
        body: dict[str, Any] = {"vector": embedding, "topK": k, "namespace": self.NAMESPACE, "includeMetadata": False, "includeValues": False}
        if flt:
            body["filter"] = flt
        return [m["id"] for m in (await self._req("POST", f"https://{host}/query", body)).get("matches", [])], exact

    async def drop_index(self, index_name: str) -> None:
        await self._req("DELETE", f"{self.CONTROL}/indexes/{index_name}", ok404=True)
        self._hosts.pop(index_name, None)
