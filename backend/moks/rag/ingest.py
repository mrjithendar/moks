"""Ingestion pipeline: GitHub repositories, uploaded documents and approved postmortems.

GitHub sync is incremental (git blob SHA per path plus the pipeline version), works through a
persistent inventory in bounded batches, propagates deletions only from a complete listing, records
excluded files with a reason, and stamps every document with repository, path, revision, service,
scope and owner.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..connectors.base import ConnectorError
from ..connectors.git import GitConnector
from ..models import Document, Incident, KnowledgeSource, KnowledgeSyncItem
from ..observability import INDEXING_DOCUMENTS
from ..redaction import redact
from . import service
from .chunking import PIPELINE_VERSION

log = logging.getLogger("moks.rag.ingest")

DEFAULT_INCLUDE = ["**/*.md", "docs/**", "runbooks/**", "**/*.yaml", "**/*.yml", "**/Dockerfile", ".github/workflows/*",
                   "**/Chart.yaml", "**/values*.yaml", "**/*.tf"]
DEFAULT_EXCLUDE = ["**/node_modules/**", "**/vendor/**", "**/dist/**", "**/.git/**", "**/package-lock.json", "**/*.lock",
                   "**/*.min.js", "**/*.svg", "**/*.png", "**/*.jpg"]
BATCH_FILES = 500  # files read per pass: the job runs again until the listing is finished
PIN_HOURS = 24  # a pinned revision older than this is listed afresh, keeping progress on unchanged files
MAX_FILE_BYTES = 300_000
BLOB_DELAY_SECONDS = 0.1  # GitHub asks for serial, spaced requests
DEFAULT_TOKEN = "MOKS_CRED_GITHUB_TOKEN"


def _match(path: str, patterns: list[str]) -> bool:
    for p in patterns:
        if fnmatchcase(path, p) or (p.startswith("**/") and fnmatchcase(path, p[3:])):
            return True
    return False


def service_for(path: str, mapping: dict[str, str]) -> str | None:
    """config.services maps path prefixes to service names, e.g. {"services/cart/": "cart"}."""
    best = None
    for prefix, svc in mapping.items():
        if path.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, svc)
    return best[1] if best else None


def _cred(src: KnowledgeSource) -> dict | None:
    # Sources without their own credential share one token: unauthenticated GitHub allows 60 requests an hour.
    cred = src.credential_ref if (src.credential_ref or {}).get("type") not in (None, "none") else None
    if cred is None and os.environ.get(DEFAULT_TOKEN):
        cred = {"type": "env", "name": DEFAULT_TOKEN}
    return cred


def _safe_error(exc: Exception) -> str:
    return redact(str(getattr(exc, "message", exc)))[:300]


async def _take_inventory(db: AsyncSession, src: KnowledgeSource, git: GitConnector, repo: str, cfg: dict) -> dict[str, Any]:
    """Records every wanted file at one pinned revision, what still needs reading, and, only from a
    complete listing, what has gone."""
    sha, files, listing = await git.provider.inventory(repo, cfg.get("ref"))
    include, exclude = cfg.get("include") or DEFAULT_INCLUDE, (cfg.get("exclude") or []) + DEFAULT_EXCLUDE
    wanted = {f["path"]: f for f in files if _match(f["path"], include) and not _match(f["path"], exclude)}
    existing = {d.path: d for d in (await db.execute(select(Document).where(Document.source_id == src.id))).scalars()}
    items = {i.path: i for i in (await db.execute(select(KnowledgeSyncItem).where(KnowledgeSyncItem.source_id == src.id))).scalars()}
    now = datetime.now(timezone.utc)
    too_large: list[str] = []
    excluded = 0
    for path, f in wanted.items():
        item = items.get(path)
        if item is None:
            item = KnowledgeSyncItem(source_id=src.id, path=path, blob_sha=f["sha"], size_bytes=f.get("size", 0), revision=sha, state="pending", attempts=0)
            db.add(item)
        same_blob = item.blob_sha == f["sha"]
        item.blob_sha, item.size_bytes, item.revision = f["sha"], f.get("size", 0), sha
        doc = existing.get(path)
        current = doc is not None and doc.content_hash == f"git:{f['sha']}" and doc.pipeline_version == PIPELINE_VERSION and doc.status in ("active", "excluded")
        if f.get("size", 0) > MAX_FILE_BYTES:
            too_large.append(path)
            excluded += 1
            await service.exclude_document(db, src, path, f"larger than the {MAX_FILE_BYTES // 1000} KB limit", f"git:{f['sha']}", sha)
            item.state, item.last_error = "excluded", "larger than the size limit"
        elif current:
            item.state, item.last_error = ("excluded" if doc.status == "excluded" else "done"), None
            doc.checked_at, doc.revision = now, sha
        else:
            item.state = "pending"
            if not same_blob:
                item.attempts, item.last_error = 0, None
    deleted = 0
    # Only a complete listing can prove a file is gone. An empty one from a repository we had
    # documents for is far more likely a bad ref or a filter mistake than a real wipe.
    trusted = listing == "complete" and bool(wanted or not [d for d in existing.values() if d.status != "deleted"])
    if trusted:
        for path, doc in existing.items():
            if path not in wanted and doc.status != "deleted":
                await service.remove_document(db, doc)
                deleted += 1
        await db.execute(sa_delete(KnowledgeSyncItem).where(KnowledgeSyncItem.source_id == src.id, KnowledgeSyncItem.path.not_in(list(wanted) or [""])))
    src.sync_cursor = {"revision": sha, "listing": listing, "state": "ingesting", "started_at": now.isoformat(), "deletions_applied": trusted, "deleted": deleted,
                       "too_large": too_large[:20], "added": 0, "updated": 0}
    await db.commit()
    return src.sync_cursor


async def _read_batch(db: AsyncSession, src: KnowledgeSource, git: GitConnector, repo: str, cfg: dict, cursor: dict) -> None:
    """A bounded batch, least recently tried first, so a file that keeps failing cannot hold back those after it."""
    sha = cursor["revision"]
    rows = (await db.execute(select(KnowledgeSyncItem).where(KnowledgeSyncItem.source_id == src.id, KnowledgeSyncItem.state == "pending")
                             .order_by(KnowledgeSyncItem.last_tried_at.asc().nulls_first(), KnowledgeSyncItem.path).limit(BATCH_FILES))).scalars().all()
    for item in rows:
        path, blob_sha = item.path, item.blob_sha
        await asyncio.sleep(BLOB_DELAY_SECONDS)
        item.last_tried_at = datetime.now(timezone.utc)
        try:
            text = await git.provider.blob(repo, blob_sha)
        except ConnectorError as exc:
            if exc.kind == "rate_limited":
                await db.commit()
                raise  # every later file would fail too; finished files are committed, so the retry resumes here
            item.attempts += 1
            item.last_error, item.state = _safe_error(exc), "failed"  # tried again at the next listing
            log.warning("skip file", extra={"event": "ingest_skip", "errorType": exc.kind})
            await db.commit()
            continue
        known = (await db.execute(select(Document.id).where(Document.source_id == src.id, Document.path == path))).first() is not None
        doc = await service.index_document(db, src, path, text, revision=sha, content_hash=f"git:{blob_sha}", url=git.provider.blob_url(repo, path, sha),
                                           extra={"service": service_for(path, cfg.get("services") or {}) or src.service})
        item.state, item.last_error = ("excluded" if doc.status == "excluded" else "done"), doc.excluded_reason if doc.status == "excluded" else None
        cursor["updated" if known else "added"] = cursor.get("updated" if known else "added", 0) + 1
        await db.commit()


async def sync_github(db: AsyncSession, src: KnowledgeSource, transport=None) -> dict[str, Any]:
    """One bounded pass. A listing pins a revision and is finished before the next listing starts, so
    a repository of any size completes over several passes, each resuming from committed progress."""
    cfg = src.config or {}
    repo = cfg.get("repository")
    if not repo:
        raise ConnectorError("config", "github source needs config.repository (owner/name)")
    git = GitConnector({"provider": "github", "repositories": [repo], "api_url": cfg.get("api_url"), "web_url": cfg.get("web_url")}, _cred(src), transport=transport)
    try:
        cursor = dict(src.sync_cursor or {})
        resuming = cursor.get("state") == "ingesting" and datetime.now(timezone.utc) - datetime.fromisoformat(cursor["started_at"]) < timedelta(hours=PIN_HOURS)
        if not resuming:
            source_id = src.id
            try:
                cursor = dict(await _take_inventory(db, src, git, repo, cfg))
            except ConnectorError:
                await db.rollback()
                row = await db.get(KnowledgeSource, source_id)
                row.sync_cursor = {**(row.sync_cursor or {}), "listing": "failed"}
                await db.commit()
                raise
        await _read_batch(db, src, git, repo, cfg, cursor)
        counts = dict((await db.execute(select(KnowledgeSyncItem.state, func.count()).where(KnowledgeSyncItem.source_id == src.id).group_by(KnowledgeSyncItem.state))).all())
        pending, failed, excluded, done = (counts.get(k, 0) for k in ("pending", "failed", "excluded", "done"))
        discovered = pending + failed + excluded + done
        content = "in_progress" if pending else "partial" if failed else "complete"
        cursor.update(state="ingesting" if pending else "done", content=content, discovered=discovered, processed=done + excluded, excluded=excluded, failed=failed, remaining=pending)
        src.sync_cursor = cursor
        trusted = cursor.get("deletions_applied", False)
        if content == "complete":
            src.revision = cursor["revision"]
        problem = (f"{pending} of {discovered} files still to read" if pending else f"{failed} files could not be read" if failed
                   else None if trusted else "the listing looked incomplete, so nothing was deleted")
        await service.mark_indexed_now(db, src, "ok" if problem is None else "in_progress" if pending else "partial", problem)
        await db.commit()
        INDEXING_DOCUMENTS.labels(src.name, "active").set(done)
        INDEXING_DOCUMENTS.labels(src.name, "excluded").set(excluded)
        return {"revision": cursor["revision"], "listing": cursor.get("listing"), "content": content, "complete": content == "complete" and trusted, "files_considered": discovered, "discovered": discovered, "processed": done + excluded,
                "remaining": pending, "added": cursor.get("added", 0), "updated": cursor.get("updated", 0), "unchanged": max(done - cursor.get("added", 0) - cursor.get("updated", 0), 0),
                "deleted": cursor.get("deleted", 0), "excluded": excluded, "failed": failed, "deletions_skipped": not trusted, "skipped_too_large": cursor.get("too_large", [])}
    finally:
        await git.aclose()


async def add_upload(db: AsyncSession, src: KnowledgeSource, filename: str, content: bytes, scope: dict | None = None) -> Document:
    if src.type != "upload":
        raise ConnectorError("bad_request", "uploads go to an 'upload' source")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise ConnectorError("bad_request", "only UTF-8 text documents are supported (Markdown, text, YAML, JSON, code)") from None
    doc = await service.index_document(db, src, filename, text, revision=None, url=None, extra=scope or {})
    await service.mark_indexed_now(db, src, "ok")
    return doc


async def incident_source(db: AsyncSession) -> KnowledgeSource:
    src = (await db.execute(select(KnowledgeSource).where(KnowledgeSource.name == "incident-postmortems"))).scalar_one_or_none()
    if src is None:
        src = KnowledgeSource(name="incident-postmortems", type="incident", config={}, owner="MOKS")
        db.add(src)
        await db.flush()
    return src


async def add_postmortem(db: AsyncSession, inc: Incident, env_name: str, markdown: str, public_url: str) -> Document:
    """Approved postmortems become retrievable, scoped to the incident's environment and namespace."""
    src = await incident_source(db)
    return await service.index_document(
        db, src, f"incidents/{inc.id}.md", markdown, revision=None, url=f"{public_url.rstrip('/')}/incidents/{inc.id}",
        extra={"environment": env_name, "namespace": inc.namespace, "title": f"Postmortem: {inc.title}", "owner": "incident review"})
