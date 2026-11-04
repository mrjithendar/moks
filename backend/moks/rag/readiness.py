"""Whether a knowledge source can be retrieved now, and if not, why.

Two things are easy to confuse: the upstream sync (every wanted file read at one revision) and indexing (what
the active search backend holds). A sync can finish while an external index is still applying the outbox, or
with documents waiting for review, and neither of those is searchable yet. This module keeps them apart.

Figures about a source are for those who administer sources. People who only read knowledge get hints about
sources they may read, never about others: not their names, not their counts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Chunk, Document, DocumentIndexState, IndexOutbox, KnowledgeSource, KnowledgeSyncItem
from ..rbac import Principal
from . import access, ingest, service

REASON_TEXT = {
    "source_inactive": "The source is not active, so nothing from it is retrieved.",
    "source_disabled": "The source is disabled, so it is not synced.",
    "never_synced": "The source has not been synced yet.",
    "sync_failed": "The last sync failed before it finished listing the repository.",
    "sync_incomplete": "The sync is incomplete: files are still waiting to be read.",
    "sync_partial": "Some files could not be read in the last sync.",
    "no_active_index": "There is no active search index.",
    "indexing_pending": "Indexing is pending: the search backend has not applied every change yet.",
    "indexing_failed": "Some index operations failed and ran out of retries.",
    "awaiting_review": "Documents are waiting for review (draft or quarantined) and are not retrieved until approved.",
    "no_searchable_documents": "No document of this source is searchable.",
    "stale_excluded": "Documents past the source's freshness limit are excluded from retrieval.",
    "file_excluded": "The file is excluded by the ingestion policy.",
    "file_content_excluded": "The file was read but excluded from the index.",
    "file_not_found": "The file is not in the source's latest listing.",
    "file_pending": "The file is waiting to be read.",
    "file_failed": "The file could not be read.",
    "outside_scope": "The source is outside the requested environment or namespace.",
    "no_access": "You hold no knowledge binding that covers this source's scope.",
    "no_results": "No relevant authorized passages were found.",
}


def _reason(code: str, detail: str | None = None) -> dict:
    return {"code": code, "message": REASON_TEXT[code], **({"detail": detail} if detail else {})}


async def source_health(db: AsyncSession, src: KnowledgeSource) -> dict[str, Any]:
    """Upstream sync, indexing and retrieval readiness of one source, with the reasons it is not ready."""
    cur = src.sync_cursor or {}
    items = dict((await db.execute(select(KnowledgeSyncItem.state, func.count()).where(KnowledgeSyncItem.source_id == src.id).group_by(KnowledgeSyncItem.state))).all())
    docs = dict((await db.execute(select(Document.status, func.count()).where(Document.source_id == src.id).group_by(Document.status))).all())
    lifecycles = dict((await db.execute(select(Document.lifecycle, func.count()).where(Document.source_id == src.id, Document.status == "active").group_by(Document.lifecycle))).all())
    vectors = dict((await db.execute(select(Document.vector_state, func.count()).where(Document.source_id == src.id, Document.status == "active").group_by(Document.vector_state))).all())
    behind = (await db.execute(select(func.count()).select_from(Document).where(Document.source_id == src.id, Document.content_revision != Document.indexed_revision))).scalar_one()
    ids = select(Document.id).where(Document.source_id == src.id).scalar_subquery()
    outbox = dict((await db.execute(select(IndexOutbox.status, func.count()).where(IndexOutbox.document_id.in_(ids), IndexOutbox.status != "done").group_by(IndexOutbox.status))).all())
    idx = await service.active_index(db)
    searchable_docs = searchable_chunks = 0
    readable = (Document.source_id == src.id, Document.status == "active", Document.lifecycle == "active")
    if idx is not None and src.lifecycle == "active":
        if service.external():  # what the external index holds at the document's current revision
            row = (await db.execute(select(func.count(), func.coalesce(func.sum(DocumentIndexState.chunks), 0)).select_from(Document)
                                    .join(DocumentIndexState, (DocumentIndexState.document_id == Document.id) & (DocumentIndexState.index_id == idx.id))
                                    .where(*readable, DocumentIndexState.revision == Document.content_revision, Document.indexed_revision == Document.content_revision))).one()
        else:  # PostgreSQL serves keyword search from the chunks table itself
            row = (await db.execute(select(func.count(func.distinct(Document.id)), func.count(Chunk.id)).select_from(Document).join(Chunk, Chunk.document_id == Document.id).where(*readable))).one()
        searchable_docs, searchable_chunks = int(row[0]), int(row[1])
    now = datetime.now(timezone.utc)
    stale = 0
    if src.stale_policy == "exclude" and src.stale_after_days:
        stale = (await db.execute(select(func.count()).select_from(Document).where(*readable, (Document.checked_at.is_(None)) | (Document.checked_at < now - timedelta(days=src.stale_after_days))))).scalar_one()
    pending, failed, excluded, done = (items.get(k, 0) for k in ("pending", "failed", "excluded", "done"))
    upstream = ("failed" if cur.get("listing") == "failed" else "never" if src.type in ("github", "web") and not src.last_sync_at and not cur
                else "in_progress" if pending or cur.get("state") == "ingesting" else "partial" if failed else "complete")
    if src.type not in ("github", "web"):
        upstream = "not_applicable"
    dead, waiting = outbox.get("dead", 0), outbox.get("pending", 0)
    indexing = "no_index" if idx is None else "failed" if dead else "pending" if waiting or behind else "complete"
    reasons: list[dict] = []
    if src.lifecycle != "active":
        reasons.append(_reason("source_inactive", f"lifecycle is {src.lifecycle}"))
    if not src.enabled and src.type in ("github", "web"):
        reasons.append(_reason("source_disabled"))
    if upstream == "never":
        reasons.append(_reason("never_synced"))
    elif upstream == "failed":
        reasons.append(_reason("sync_failed", src.last_error))
    elif upstream == "in_progress":
        reasons.append(_reason("sync_incomplete", f"{pending} of {pending + failed + excluded + done} files still to read"))
    elif upstream == "partial":
        reasons.append(_reason("sync_partial", f"{failed} files failed"))
    if idx is None:
        reasons.append(_reason("no_active_index"))
    elif indexing == "failed":
        reasons.append(_reason("indexing_failed", f"{dead} index operations ran out of retries"))
    elif indexing == "pending":
        reasons.append(_reason("indexing_pending", f"{max(waiting, behind)} document changes not yet in the index"))
    review = lifecycles.get("draft", 0) + lifecycles.get("quarantined", 0)
    if review:
        reasons.append(_reason("awaiting_review", f"{lifecycles.get('quarantined', 0)} quarantined, {lifecycles.get('draft', 0)} draft"))
    if stale:
        reasons.append(_reason("stale_excluded", f"{stale} documents not confirmed in {src.stale_after_days} days"))
    if not searchable_docs:
        reasons.append(_reason("no_searchable_documents"))
    return {
        "source": {"id": str(src.id), "name": src.name, "type": src.type, "lifecycle": src.lifecycle, "enabled": src.enabled,
                   "scope": {"environment": src.scope_environment, "namespace": src.scope_namespace, "shared_globally": src.shared_globally},
                   "profiles": (src.config or {}).get("profiles") or [], "review_required": src.review_required, "stale_policy": src.stale_policy, "stale_after_days": src.stale_after_days},
        "upstream": {"state": upstream, "last_success_at": src.last_success_at.isoformat() if src.last_success_at else None, "last_attempt_at": src.last_attempt_at.isoformat() if src.last_attempt_at else None,
                     "revision": src.revision, "pinned_revision": cur.get("revision"), "listing": cur.get("listing"), "last_error": src.last_error,
                     "files": {"listed": cur.get("listed"), "included": pending + failed + excluded + done, "excluded_by_policy": cur.get("policy_excluded"),
                               "excluded_by_content": excluded, "failed": failed, "pending": pending, "done": done},
                     "policy_sample": (cur.get("policy_sample") or [])[:20], "deletions_applied": cur.get("deletions_applied")},
        "documents": {"by_status": docs, "active": lifecycles.get("active", 0), "draft": lifecycles.get("draft", 0), "quarantined": lifecycles.get("quarantined", 0), "archived": lifecycles.get("archived", 0),
                      "vectors": vectors},
        "indexing": {"state": indexing, "backend": service.backend_name(), "index": idx.index_name if idx else None, "mode": ("hybrid" if idx.dimensions else "keyword-only") if idx else None,
                     "outbox_pending": waiting, "outbox_dead": dead, "documents_behind": behind},
        "searchable": {"documents": searchable_docs, "chunks": searchable_chunks},
        "ready": not [r for r in reasons if r["code"] in ("source_inactive", "never_synced", "sync_failed", "no_active_index", "no_searchable_documents")],
        "reasons": reasons,
    }


async def explain_path(db: AsyncSession, src: KnowledgeSource, path: str) -> list[dict]:
    """Why one file of a source is or is not searchable, from the policy, the inventory and the document row."""
    if src.type == "github":
        verdict = ingest.explain(path, src.config or {})
        if not verdict["included"]:
            return [_reason("file_excluded", verdict["reason"])]
    item = await db.get(KnowledgeSyncItem, (src.id, path))
    doc = (await db.execute(select(Document).where(Document.source_id == src.id, Document.path == path))).scalar_one_or_none()
    if item is not None and item.state == "pending":
        return [_reason("file_pending")]
    if item is not None and item.state == "failed":
        return [_reason("file_failed", item.last_error)]
    if doc is None:
        return [_reason("file_not_found")]
    if doc.status == "excluded":
        return [_reason("file_content_excluded", doc.excluded_reason)]
    if doc.status != "active":
        return [_reason("file_not_found", f"document is {doc.status}")]
    out = []
    if doc.lifecycle != "active":
        out.append(_reason("awaiting_review", f"document is {doc.lifecycle}"))
    if doc.content_revision != doc.indexed_revision:
        out.append(_reason("indexing_pending", "this document's latest change is not in the index yet"))
    return out


async def test_source(db: AsyncSession, principal: Principal, src: KnowledgeSource, query: str, environment: str | None, namespace: str | None, path: str | None = None, k: int = 6) -> dict:
    """The same search an investigation runs (kb_search: the person's grants, the request's environment and
    namespace), narrowed to one source. What comes back is what an investigation would see."""
    result = await service.search(db, principal, query, k, environment=environment, namespace=None if namespace in (None, "*") else namespace, neighbors=False, sources=[str(src.id)])
    reasons: list[dict] = []
    if not result["results"]:
        health = await source_health(db, src)
        reasons += [r for r in health["reasons"] if r["code"] not in ("stale_excluded",)]
        if not access.can_read(principal, src.scope_environment, src.scope_namespace):
            reasons.append(_reason("no_access", f"source scope {src.scope_environment}/{src.scope_namespace}"))
        elif not access.in_request(environment, None if namespace in (None, "*") else namespace, src.scope_environment, src.scope_namespace):
            reasons.append(_reason("outside_scope", f"source scope {src.scope_environment}/{src.scope_namespace}; requested {environment or '*'}/{namespace or '*'}"))
        if path:
            reasons += await explain_path(db, src, path)
        if not reasons:
            reasons.append(_reason("no_results", result.get("evidence", {}).get("reason")))
    return {"query": query, "scope": {"environment": environment, "namespace": namespace}, "mode": result.get("mode"), "fallback_reason": result.get("fallback_reason"),
            "evidence": result.get("evidence"), "results": result["results"], "reasons": reasons, "latency_ms": result.get("latency_ms"),
            "note": "Runs the investigation's search: the same grants, scope, lifecycle and freshness rules."}


async def hints(db: AsyncSession, principal: Principal, environment: str, namespace: str | None) -> list[str]:
    """For an investigation that found nothing: what is wrong with sources this person can read in this scope.
    Counts of readable sources only; a source outside their grants does not exist here."""
    idx = await service.active_index(db)
    if idx is None:
        return [REASON_TEXT["no_active_index"]]
    rows = (await db.execute(select(KnowledgeSource).where(KnowledgeSource.realm == access.realm(), KnowledgeSource.lifecycle != "deleted"))).scalars().all()
    mine = [s for s in rows[:200] if access.can_read(principal, s.scope_environment, s.scope_namespace) and access.in_request(environment, namespace, s.scope_environment, s.scope_namespace)]
    if not mine:
        return ["No knowledge source you can read covers this environment and namespace."]
    out: list[str] = []
    counts = {"sync": 0, "index": 0, "review": 0, "inactive": 0}
    for s in mine:
        h = await source_health(db, s)
        codes = {r["code"] for r in h["reasons"]}
        counts["sync"] += bool(codes & {"never_synced", "sync_failed", "sync_incomplete"})
        counts["index"] += bool(codes & {"indexing_pending", "indexing_failed"})
        counts["review"] += "awaiting_review" in codes
        counts["inactive"] += "source_inactive" in codes
    if counts["sync"]:
        out.append(f"{counts['sync']} of the {len(mine)} sources you can read here have not finished syncing.")
    if counts["index"]:
        out.append(f"{counts['index']} of them have changes the search index has not applied yet.")
    if counts["review"]:
        out.append(f"{counts['review']} of them have documents waiting for review, which are not retrieved until approved.")
    if counts["inactive"]:
        out.append(f"{counts['inactive']} of them are not active.")
    return out
