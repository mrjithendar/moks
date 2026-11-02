"""Retrieval backends. PostgreSQL is always the source of truth for documents and chunks; a
backend only indexes and searches them.

postgres   - full-text (websearch_to_tsquery, ts_rank_cd) and, when the pgvector extension is
             installed and an embedding index is active, cosine k-NN; fused with RRF.
opensearch - one index per embedding version (moks-chunks-<version>), BM25 multi_match plus
             knn_vector; fused client-side with RRF so no search pipeline plugin is required.
             Self-hosted (basic auth, one identity per job) or an Amazon OpenSearch Service domain (SigV4).
pinecone   - vectors only, one index per embedding version. Keyword search stays in PostgreSQL, which
             holds the text. Pinecone receives vectors, document ids and scope names, never chunk text.
"""
from __future__ import annotations

import asyncio
import json
import ssl
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..connectors.base import ConnectorError
from ..observability import OPENSEARCH_BULK_ITEMS, OPENSEARCH_SEARCH_FAILURES, OPENSEARCH_SEARCH_SECONDS
from . import access

RRF_K = 60
MAPPING_VERSION = 2
INDEX_PREFIX = "moks-chunks-"
SEARCH_COOLDOWN_SECONDS = 30.0
# Everything a chunk is filtered, cited or explained by. Strict, so a typo or an unplanned field is
# refused at write time instead of quietly creating a mapping nobody chose.
_KEYWORDS = ("chunk_id", "document_id", "source_id", "realm", "environment", "namespace", "repository", "service", "applies_to", "owner", "team",
             "lifecycle", "classification", "trust", "kind", "git_revision", "content_hash", "text_hash", "embedding_model")
_DATES = ("document_updated_at", "checked_at", "indexed_at")
_INTEGERS = ("ord", "start_line", "end_line", "start_offset", "end_offset", "embedding_dimensions")
SEARCH_FIELDS = ["heading^2", "text", "path^1.5", "heading.exact^3", "text.exact^3", "path.exact^3"]
# Splits on spaces and punctuation that surrounds a name, never inside one: moks-api, MOKS_RAG_BACKEND,
# deploy/helm/values.yaml and CrashLoopBackOff stay whole, and match in any case.
_ANALYSIS = {
    "tokenizer": {"moks_identifier": {"type": "pattern", "pattern": "[\\s,;()\\[\\]{}\"'`<>=|]+"}},
    "normalizer": {"moks_lowercase": {"type": "custom", "filter": ["lowercase"]}},
    "analyzer": {"moks_identifier": {"type": "custom", "tokenizer": "moks_identifier", "filter": ["lowercase"]}},
}
_EXACT = {"exact": {"type": "text", "analyzer": "moks_identifier", "norms": False}}


def index_body(dims: int | None, model_key: str = "") -> dict:
    """The index definition for one generation. One shard and no replica: a single node cannot place a
    replica, so one would only hold the cluster yellow. It is redundancy this deployment does not have."""
    props: dict[str, Any] = {name: {"type": "keyword"} for name in _KEYWORDS}
    props |= {name: {"type": "date"} for name in _DATES} | {name: {"type": "integer"} for name in _INTEGERS}
    props |= {"content_revision": {"type": "long"}, "has_vector": {"type": "boolean"},
              "identifiers": {"type": "keyword", "normalizer": "moks_lowercase", "ignore_above": 200},
              "path": {"type": "text", "analyzer": "english", "fields": {"raw": {"type": "keyword"}, **_EXACT}},
              "heading": {"type": "text", "analyzer": "english", "fields": _EXACT},
              "text": {"type": "text", "analyzer": "english", "fields": _EXACT}}
    settings: dict[str, Any] = {"index": {"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "1s"}, "analysis": _ANALYSIS}
    if dims:
        props["embedding"] = {"type": "knn_vector", "dimension": dims, "method": {"name": "hnsw", "engine": "lucene", "space_type": "cosinesimil"}}
        settings["index"]["knn"] = True
    return {"settings": settings, "mappings": {"dynamic": "strict", "_meta": {"mapping_version": MAPPING_VERSION, "model_key": model_key, "dimensions": dims}, "properties": props}}


@dataclass
class BulkResult:
    """What the server said about each item. HTTP 200 only means the request was read."""
    written: int = 0
    vectors: int = 0
    superseded: int = 0  # a newer revision was already there: correct, not a failure
    failed: list[str] = field(default_factory=list)
    first_error: str | None = None


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
    params["realm"] = access.realm()
    return f"d.status = 'active' AND d.lifecycle = 'active' AND s.lifecycle = 'active' AND d.realm = :realm AND s.realm = :realm AND {access.sql(found, params)}"


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
    keeps_old_chunks = True  # a rewrite replaces chunks without a moment where the document has none

    def __init__(self, url: str, username: str = "", password: str = "", verify: bool = True,
                 transport: httpx.AsyncBaseTransport | None = None, aws_region: str = "", ca_file: str = "",
                 index_auth: tuple[str, str] | None = None, lifecycle_auth: tuple[str, str] | None = None, search_timeout: float = 3.0):
        if not url:
            raise ConnectorError("config", "MOKS_OPENSEARCH_URL is required for the opensearch backend")
        if aws_region and username:
            raise ConnectorError("config", "set MOKS_OPENSEARCH_AWS_REGION or MOKS_OPENSEARCH_USERNAME, not both")
        if aws_region and not url.startswith("https://"):
            raise ConnectorError("config", "an Amazon OpenSearch Service endpoint must use https")
        if username and url.startswith("http://") and not url.startswith("http://localhost"):
            raise ConnectorError("config", "OpenSearch credentials must not travel over plain http; use an https URL")
        if ca_file and not verify:
            raise ConnectorError("config", "MOKS_OPENSEARCH_CA_FILE is set but certificate verification is off")
        tls: Any = verify
        if ca_file:
            try:
                tls = ssl.create_default_context(cafile=ca_file)
            except (OSError, ssl.SSLError) as exc:
                raise ConnectorError("config", f"cannot read the OpenSearch CA file ({type(exc).__name__})") from None
        base = url.rstrip("/")
        auth: Any = SigV4(aws_region) if aws_region else (username, password) if username else None

        def client(who: Any, timeout: float) -> httpx.AsyncClient:
            return httpx.AsyncClient(base_url=base, auth=who, verify=tls, timeout=timeout, transport=transport)

        self.client = client(auth, 20)
        self.search_client = client(auth, search_timeout)
        self.index_client = client(index_auth, 60) if index_auth else self.client
        self.lifecycle_client = client(lifecycle_auth, 60) if lifecycle_auth else self.client
        self._quiet_until = 0.0

    async def aclose(self) -> None:
        for c in {self.client, self.search_client, self.index_client, self.lifecycle_client}:
            await c.aclose()

    async def _req(self, method: str, path: str, body: Any = None, ok404: bool = False, ndjson: str | None = None, via: httpx.AsyncClient | None = None) -> dict:
        client = via or self.client
        try:
            if ndjson is not None:
                r = await client.request(method, path, content=ndjson, headers={"Content-Type": "application/x-ndjson"})
            else:
                r = await client.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise ConnectorError("unavailable", f"cannot reach OpenSearch ({type(exc).__name__})", retryable=True) from None
        if r.status_code == 404 and ok404:
            return {}
        if r.status_code in (401, 403):
            raise ConnectorError("auth", f"OpenSearch refused {method} {path}: {r.status_code}")
        if r.status_code == 429:
            raise ConnectorError("unavailable", f"OpenSearch is rejecting requests: {method} {path}", retryable=True)
        if r.status_code >= 400:
            raise ConnectorError("unavailable" if r.status_code >= 500 else "bad_request", f"OpenSearch {method} {path}: {r.status_code} {r.text[:300]}", retryable=r.status_code >= 500)
        return r.json() if r.content else {}

    async def _search(self, kind: str, index_name: str, body: dict) -> list[str]:
        """A search that cannot wait: a user is behind it, and PostgreSQL can answer in its place. A missing
        index is a fault here, never an empty result. After a failure, this process stops asking for a moment."""
        if time.monotonic() < self._quiet_until:
            OPENSEARCH_SEARCH_FAILURES.labels(kind, "cooldown").inc()
            raise ConnectorError("unavailable", "OpenSearch failed moments ago", retryable=True)
        start = time.monotonic()
        try:
            resp = await self._req("POST", f"/{index_name}/_search", body, via=self.search_client)
        except ConnectorError as exc:
            OPENSEARCH_SEARCH_FAILURES.labels(kind, exc.kind).inc()
            self._quiet_until = time.monotonic() + SEARCH_COOLDOWN_SECONDS
            raise
        OPENSEARCH_SEARCH_SECONDS.labels(kind).observe(time.monotonic() - start)
        if (resp.get("_shards") or {}).get("failed"):
            OPENSEARCH_SEARCH_FAILURES.labels(kind, "shard").inc()
            raise ConnectorError("unavailable", "OpenSearch answered from only part of the index", retryable=True)
        return [h["_source"]["chunk_id"] for h in (resp.get("hits") or {}).get("hits", [])]

    async def health(self) -> dict:
        return await self._req("GET", "/_cluster/health", via=self.lifecycle_client)

    async def mapping(self, name: str) -> dict | None:
        resp = await self._req("GET", f"/{name}/_mapping", ok404=True, via=self.lifecycle_client)
        return (resp.get(name) or {}).get("mappings") if resp else None

    async def vector_dimensions(self, name: str) -> int | None:
        """Dimension of an existing index's vector field; None when it is keyword-only or absent."""
        props = ((await self.mapping(name)) or {}).get("properties") or {}
        return (props.get("embedding") or {}).get("dimension")

    async def ensure_index(self, name: str, dims: int | None, model_key: str = "") -> None:
        found = await self.mapping(name)
        if found is not None:
            have = ((found.get("properties") or {}).get("embedding") or {}).get("dimension")
            if have != dims:
                raise ConnectorError("config", f"OpenSearch index {name} has vector dimension {have}, expected {dims}; run a reindex")
            return
        await self._req("PUT", f"/{name}", index_body(dims, model_key), via=self.lifecycle_client)

    async def preflight(self, needed_bytes: int) -> dict:
        """Before a build starts rather than halfway through it. Reads what the node says; hostPath volumes
        have no quota, so free space here is the disk every workload shares."""
        state = await self.status()
        if not state["reachable"]:
            raise ConnectorError("unavailable", f"OpenSearch is not answering: {state.get('error')}", retryable=True)
        if state["health"] == "red":
            raise ConnectorError("config", "OpenSearch cluster health is red; fix it before building an index")
        if state["read_only"]:
            raise ConnectorError("config", "OpenSearch has a write block, usually the flood-stage disk watermark; free disk first")
        if state["disk_free_bytes"] is not None and state["disk_free_bytes"] - state.get("flood_stage_bytes", 0) < needed_bytes:
            raise ConnectorError("config", f"a new index needs about {needed_bytes} bytes and only {state['disk_free_bytes']} are free on the disk OpenSearch shares")
        return state

    async def status(self) -> dict:
        """Cluster state for the admin page and metrics, in one shape whatever fails."""
        out: dict[str, Any] = {"reachable": False, "health": None, "read_only": False, "unassigned_shards": None, "heap_used_percent": None, "disk_free_bytes": None,
                               "disk_total_bytes": None, "search_rejected": None, "write_rejected": None, "gc_seconds": None, "version": None, "error": None}
        try:
            health = await self.health()
            out |= {"reachable": True, "health": health.get("status"), "unassigned_shards": health.get("unassigned_shards")}
            out["version"] = ((await self._req("GET", "/", via=self.lifecycle_client)).get("version") or {}).get("number")
            stats = await self._req("GET", "/_nodes/stats/jvm,fs,thread_pool", via=self.lifecycle_client)
            node = next(iter((stats.get("nodes") or {}).values()), {})
            mem, disk, pools = (node.get("jvm") or {}).get("mem") or {}, (node.get("fs") or {}).get("total") or {}, node.get("thread_pool") or {}
            collectors = ((node.get("jvm") or {}).get("gc") or {}).get("collectors") or {}
            out |= {"heap_used_percent": mem.get("heap_used_percent"), "disk_free_bytes": disk.get("available_in_bytes"), "disk_total_bytes": disk.get("total_in_bytes"),
                    "search_rejected": (pools.get("search") or {}).get("rejected"), "write_rejected": (pools.get("write") or {}).get("rejected"),
                    "gc_seconds": sum(c.get("collection_time_in_millis", 0) for c in collectors.values()) / 1000}
            blocks = await self._req("GET", "/_cluster/state/blocks", via=self.lifecycle_client)
            out["read_only"] = bool((blocks.get("blocks") or {}).get("indices") or (blocks.get("blocks") or {}).get("global"))
        except ConnectorError as exc:
            out["error"] = exc.message[:300]
        return out

    async def index_chunks(self, index_name: str, rows: list[dict], embeddings: list[list[float]] | None) -> BulkResult:
        """A row carrying content_revision is fenced by it: an older write can never replace a newer one,
        and replaying the same one is harmless. Every item is read back, because a bulk request that
        answers 200 can still have refused any of its items."""
        lines, result = [], BulkResult()
        for i, r in enumerate(rows):
            doc = dict(r)
            action: dict[str, Any] = {"_index": index_name, "_id": r["chunk_id"]}
            if "content_revision" in r:
                action |= {"version": r["content_revision"], "version_type": "external_gte"}
                doc["has_vector"] = bool(embeddings)
            if embeddings:
                doc["embedding"] = embeddings[i]
            lines.append(json.dumps({"index": action}))
            lines.append(json.dumps(doc))
        if not lines:
            return result
        resp = await self._req("POST", "/_bulk?refresh=wait_for", ndjson="\n".join(lines) + "\n", via=self.index_client)
        items = [next(iter(it.values())) for it in resp.get("items") or []]
        if len(items) != len(rows):
            raise ConnectorError("bad_request", f"OpenSearch answered {len(items)} items for a bulk of {len(rows)}")
        for row, item in zip(rows, items):
            error = item.get("error")
            if error and item.get("status") == 409:
                result.superseded += 1
            elif error:
                result.failed.append(row["chunk_id"])
                result.first_error = result.first_error or json.dumps(error)[:300]
            else:
                result.written += 1
        result.vectors = result.written if embeddings else 0
        OPENSEARCH_BULK_ITEMS.labels("written").inc(result.written)
        OPENSEARCH_BULK_ITEMS.labels("superseded").inc(result.superseded)
        OPENSEARCH_BULK_ITEMS.labels("failed").inc(len(result.failed))
        if result.failed:
            raise ConnectorError("partial", f"OpenSearch refused {len(result.failed)} of {len(rows)} bulk items: {result.first_error}", retryable=True)
        return result

    async def delete_document(self, index_name: str, document_id: uuid.UUID, keep: list[str] | None = None) -> None:
        """Removes the document's chunks, or with `keep`, every chunk except those. Rewriting a document
        writes first and prunes after, so a failure in between leaves old text, never no text."""
        query: dict[str, Any] = {"bool": {"filter": [{"term": {"document_id": str(document_id)}}]}}
        if keep:
            query["bool"]["must_not"] = [{"terms": {"chunk_id": keep}}]
        resp = await self._req("POST", f"/{index_name}/_delete_by_query?refresh=true&conflicts=proceed", {"query": query}, ok404=True, via=self.index_client)
        if resp.get("failures") or resp.get("version_conflicts"):
            raise ConnectorError("partial", f"OpenSearch left chunks of {document_id} undeleted ({resp.get('version_conflicts', 0)} conflicts)", retryable=True)

    async def update_scope(self, index_name: str, document_id: uuid.UUID, environment: str, namespace: str) -> None:
        await self._req("POST", f"/{index_name}/_update_by_query?refresh=true&conflicts=proceed", {
            "query": {"term": {"document_id": str(document_id)}},
            "script": {"lang": "painless", "source": "ctx._source.environment = params.e; ctx._source.namespace = params.n",
                       "params": {"e": environment, "n": namespace}}}, ok404=True, via=self.index_client)

    async def count_document(self, index_name: str, document_id: uuid.UUID) -> int | None:
        resp = await self._req("POST", f"/{index_name}/_count", {"query": {"term": {"document_id": str(document_id)}}}, ok404=True, via=self.index_client)
        return resp.get("count") if resp else 0

    async def count(self, index_name: str, query: dict | None = None) -> int | None:
        """Chunks in an index, None when it does not exist."""
        resp = await self._req("POST", f"/{index_name}/_count", {"query": query} if query else None, ok404=True, via=self.lifecycle_client)
        return resp.get("count") if resp else None

    @staticmethod
    def _filter(found: list[access.Grant], exclude: dict | None) -> tuple[dict | None, bool]:
        """Grants, plus what PostgreSQL says is not searchable right now. Those ids are read per search,
        so a quarantine takes effect at commit, whatever the index last recorded for the document."""
        flt, exact = access.opensearch(found)
        terms = [{"terms": {field: ids}} for field, ids in (("source_id", (exclude or {}).get("sources")), ("document_id", (exclude or {}).get("documents"))) if ids]
        if not terms:
            return flt, exact
        return {"bool": {"filter": [flt] if flt else [], "must_not": terms}}, exact

    async def keyword(self, index_name: str, query: str, found: list[access.Grant], k: int, exclude: dict | None = None, tokens: list[str] | None = None) -> tuple[list[str], bool]:
        flt, exact = self._filter(found, exclude)
        should: list[dict] = [{"multi_match": {"query": query, "fields": SEARCH_FIELDS}}]
        if tokens:
            should.append({"terms": {"identifiers": [t.lower() for t in tokens], "boost": 4.0}})  # lower() matches the normalizer: terms skips it on some versions
        body = {"size": k, "_source": ["chunk_id"], "track_total_hits": False, "timeout": "2500ms",
                "query": {"bool": {"should": should, "minimum_should_match": 1, "filter": [flt] if flt else []}}}
        return await self._search("keyword", index_name, body), exact

    async def vector(self, index_name: str, embedding: list[float], found: list[access.Grant], k: int, exclude: dict | None = None) -> tuple[list[str], bool]:
        flt, exact = self._filter(found, exclude)
        knn: dict[str, Any] = {"vector": embedding, "k": k}
        if flt:
            knn["filter"] = flt  # applied during the search, so unreadable neighbours do not use up k
        return await self._search("vector", index_name, {"size": k, "_source": ["chunk_id"], "track_total_hits": False, "query": {"knn": {"embedding": knn}}}), exact

    async def drop_index(self, index_name: str) -> None:
        if not index_name.startswith(INDEX_PREFIX):  # the lifecycle identity cannot either; this says why
            raise ConnectorError("config", f"refusing to delete {index_name}: not a MOKS knowledge index")
        await self._req("DELETE", f"/{index_name}", ok404=True, via=self.lifecycle_client)

    async def configure_snapshots(self, repository: str, policy: str, bucket: str, base_path: str, schedule: str, keep: int, retain: str) -> dict:
        """Registers the repository, which makes the node prove it can write there, and the schedule."""
        await self._req("PUT", f"/_snapshot/{repository}", {"type": "s3", "settings": {"bucket": bucket, "base_path": base_path}}, via=self.lifecycle_client)
        return await self.put_snapshot_policy(repository, policy, schedule, keep, retain)

    async def put_snapshot_policy(self, repository: str, policy: str, schedule: str, keep: int, retain: str) -> dict:
        body = {"description": "MOKS knowledge indexes", "enabled": True,
                "snapshot_config": {"repository": repository, "indices": f"{INDEX_PREFIX}*", "include_global_state": False, "ignore_unavailable": True, "partial": False},
                "creation": {"schedule": {"cron": {"expression": schedule, "timezone": "UTC"}}, "time_limit": "1h"},
                "deletion": {"schedule": {"cron": {"expression": schedule, "timezone": "UTC"}}, "condition": {"max_age": retain, "max_count": keep, "min_count": 1}, "time_limit": "1h"}}
        path = f"/_plugins/_sm/policies/{policy}"
        current = await self._req("GET", path, ok404=True, via=self.lifecycle_client)
        if current.get("_seq_no") is not None:
            path += f"?if_seq_no={current['_seq_no']}&if_primary_term={current['_primary_term']}"
            return await self._req("PUT", path, body, via=self.lifecycle_client)
        return await self._req("POST", path, body, via=self.lifecycle_client)

    async def snapshots(self, repository: str) -> dict:
        """Newest snapshot and newest good one. A repository nobody can read is a fact to show, not to hide."""
        resp = await self._req("GET", f"/_snapshot/{repository}/_all", ok404=True, via=self.lifecycle_client)
        rows = sorted(resp.get("snapshots") or [], key=lambda s: s.get("start_time_in_millis") or 0)
        good = [s for s in rows if s.get("state") == "SUCCESS"]
        return {"repository": repository, "count": len(rows), "registered": bool(resp), "latest_state": rows[-1].get("state") if rows else None,
                "latest_at": rows[-1].get("end_time") if rows else None, "last_success_at": good[-1].get("end_time") if good else None,
                "last_success_ms": good[-1].get("end_time_in_millis") if good else None}


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

    async def ensure_index(self, name: str, dims: int | None, model_key: str = "") -> None:
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

    async def vector(self, index_name: str, embedding: list[float], found: list[access.Grant], k: int, exclude: dict | None = None) -> tuple[list[str], bool]:
        host = await self._host(index_name)
        if not host:
            return [], True
        flt, exact = access.pinecone(found)
        exact = exact and not any((exclude or {}).values())  # metadata has no source id, so the final check removes these
        body: dict[str, Any] = {"vector": embedding, "topK": k, "namespace": self.NAMESPACE, "includeMetadata": False, "includeValues": False}
        if flt:
            body["filter"] = flt
        return [m["id"] for m in (await self._req("POST", f"https://{host}/query", body)).get("matches", [])], exact

    async def drop_index(self, index_name: str) -> None:
        await self._req("DELETE", f"{self.CONTROL}/indexes/{index_name}", ok404=True)
        self._hosts.pop(index_name, None)
