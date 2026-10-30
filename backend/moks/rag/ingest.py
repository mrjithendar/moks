"""Ingestion pipeline: GitHub repositories, uploaded documents and approved postmortems.

GitHub sync is incremental (git blob SHA per path), propagates deletions, records excluded files
with a reason, and stamps every document with repository, path, revision, service, scope and owner.
"""
from __future__ import annotations

import asyncio
import logging
import os
from fnmatch import fnmatchcase
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..connectors.base import ConnectorError
from ..connectors.git import GitConnector
from ..models import Document, Incident, KnowledgeSource
from ..observability import INDEXING_DOCUMENTS
from . import service

log = logging.getLogger("moks.rag.ingest")

DEFAULT_INCLUDE = ["**/*.md", "docs/**", "runbooks/**", "**/*.yaml", "**/*.yml", "**/Dockerfile", ".github/workflows/*",
                   "**/Chart.yaml", "**/values*.yaml", "**/*.tf"]
DEFAULT_EXCLUDE = ["**/node_modules/**", "**/vendor/**", "**/dist/**", "**/.git/**", "**/package-lock.json", "**/*.lock",
                   "**/*.min.js", "**/*.svg", "**/*.png", "**/*.jpg"]
MAX_FILES = 3000
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


async def sync_github(db: AsyncSession, src: KnowledgeSource, transport=None) -> dict[str, Any]:
    cfg = src.config or {}
    repo = cfg.get("repository")
    if not repo:
        raise ConnectorError("config", "github source needs config.repository (owner/name)")
    # Sources without their own credential share one token: unauthenticated GitHub allows 60 requests an hour.
    cred = src.credential_ref if (src.credential_ref or {}).get("type") not in (None, "none") else None
    if cred is None and os.environ.get(DEFAULT_TOKEN):
        cred = {"type": "env", "name": DEFAULT_TOKEN}
    git = GitConnector({"provider": "github", "repositories": [repo], "api_url": cfg.get("api_url"), "web_url": cfg.get("web_url")},
                       cred, transport=transport)
    try:
        sha, files = await git.provider.tree(repo, cfg.get("ref"))
        include, exclude = cfg.get("include") or DEFAULT_INCLUDE, (cfg.get("exclude") or []) + DEFAULT_EXCLUDE
        wanted = [f for f in files if _match(f["path"], include) and not _match(f["path"], exclude)]
        skipped_large = [f["path"] for f in wanted if f.get("size", 0) > MAX_FILE_BYTES]
        wanted = [f for f in wanted if f.get("size", 0) <= MAX_FILE_BYTES]
        listed = {f["path"] for f in wanted} | set(skipped_large)
        capped = len(wanted) > MAX_FILES
        wanted = wanted[:MAX_FILES]
        existing = {d.path: d for d in (await db.execute(select(Document).where(Document.source_id == src.id))).scalars()}
        added = updated = unchanged = excluded = failed = 0
        for f in wanted:
            doc = existing.get(f["path"])
            content_hash = f"git:{f['sha']}"
            if doc and doc.content_hash == content_hash and doc.status in ("active", "excluded"):
                unchanged += 1
                continue
            await asyncio.sleep(BLOB_DELAY_SECONDS)
            try:
                text = await git.provider.blob(repo, f["sha"])
            except ConnectorError as exc:
                if exc.kind == "rate_limited":
                    raise  # every later file would fail too; finished files are committed, so the retry resumes here
                failed += 1
                log.warning("skip file", extra={"event": "ingest_skip", "errorType": exc.kind})
                continue
            doc = await service.index_document(
                db, src, f["path"], text, revision=sha, content_hash=content_hash,
                url=git.provider.blob_url(repo, f["path"], sha),
                extra={"service": service_for(f["path"], cfg.get("services") or {}) or src.service})
            excluded += doc.status == "excluded"
            if f["path"] in existing:
                updated += 1
            else:
                added += 1
            await db.commit()
        present = {f["path"] for f in wanted}
        deleted = 0
        # Only a complete listing can prove a file is gone. An empty one from a repository we had
        # documents for is far more likely a bad ref or a filter mistake than a real wipe.
        trusted = not capped and bool(listed or not [d for d in existing.values() if d.status != "deleted"])
        if trusted:
            for path, doc in existing.items():
                if path not in listed and doc.status != "deleted":
                    await service.remove_document(db, doc)
                    deleted += 1
        if not failed:
            src.revision = sha
        problem = f"{failed} files could not be read" if failed else None if trusted else "the listing looked incomplete, so nothing was deleted"
        await service.mark_indexed_now(db, src, "partial" if problem else "ok", problem)
        INDEXING_DOCUMENTS.labels(src.name, "active").set(len(present) - excluded)
        INDEXING_DOCUMENTS.labels(src.name, "excluded").set(excluded)
        return {"revision": sha, "files_considered": len(wanted), "added": added, "updated": updated, "unchanged": unchanged,
                "deleted": deleted, "excluded": excluded, "failed": failed, "deletions_skipped": not trusted, "skipped_too_large": skipped_large[:20]}
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
