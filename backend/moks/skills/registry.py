"""The skill registry: immutable versions, published generations, and one policy for every use.

A SkillVersion is one exact SKILL.md, named by the SHA-256 of its text. A SkillGeneration is the exact
set of versions everyone should run. Git stays the place skills are reviewed: built-ins ship in the
image and custom skills in a reviewed ConfigMap. scan() registers what it finds; a built-in version not
seen before is published automatically, because reaching the image took a reviewed merge, and a
custom one waits for an administrator. Nothing is ever loaded from retrieved documents or tool output.

Every process follows the active generation from PostgreSQL, so API, workers and detection cannot
drift after a reload. A process that cannot use a new generation keeps its last valid one and reports
why (see versions.py). A generation that fails validation is never activated.

Decisions on active work, recorded here because they are policy:
  - Disabling a skill stops it at the next step: the steps already run stay as evidence, the remaining
    ones are recorded as skipped, and it proposes no actions. Detection stops at the next tick; open
    incidents stay open, because silence from a disabled rule proves nothing.
  - Replacing a skill does not touch a run in progress: it finishes on the version it pinned.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatchcase

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..db import session_scope
from ..models import SkillGeneration, SkillPolicy, SkillVersion
from . import Skill, SkillLibrary, load, parse, replace_library
from .validate import validate

log = logging.getLogger("moks.skills")
LOCK = 7_203_115
REFRESH_SECONDS = 5
_checked = 0.0


class PublishError(Exception):
    pass


@dataclass
class Policy:
    """Administrator decisions, read fresh wherever a skill is about to be used."""
    off: set[str] = field(default_factory=set)
    environments: dict[str, list[str]] = field(default_factory=dict)


async def policy(db: AsyncSession) -> Policy:
    rows = (await db.execute(select(SkillPolicy))).scalars().all()
    return Policy({r.name for r in rows if not r.enabled}, {r.name: r.environments for r in rows if r.environments})


def allowed(sk: Skill, environment: str | None, view: Policy) -> tuple[bool, str | None]:
    """The one answer to "may this skill be used here", for selection, steps, detection, dry runs and remediation."""
    if sk.state != "published":
        return False, f"version {sk.meta.version} is {sk.state}, not published"
    if sk.name in view.off:
        return False, "disabled by an administrator"
    if environment and not any(fnmatchcase(environment, p) for p in sk.meta.environments):
        return False, f"does not apply to {environment}"
    limit = view.environments.get(sk.name)
    if environment and limit and not any(fnmatchcase(environment, p) for p in limit):
        return False, f"limited by policy to {', '.join(limit)}"
    return True, None


def manifest_digest(manifest: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


async def _active(db: AsyncSession) -> SkillGeneration | None:
    return (await db.execute(select(SkillGeneration).where(SkillGeneration.status == "active").order_by(SkillGeneration.generation.desc()).limit(1))).scalars().first()


async def _activate(db: AsyncSession, manifest: dict[str, str], reason: str, actor: uuid.UUID | None) -> SkillGeneration:
    """Refuses a set that could not run. The current generation stays active if this raises."""
    rows = (await db.execute(select(SkillVersion).where(SkillVersion.digest.in_(list(manifest.values()))))).scalars().all()
    if len(rows) != len(set(manifest.values())):
        raise PublishError("a version in this set is not registered")
    parsed = {r.skill_id: parse(r.content, f"registry:{r.skill_id}@{r.version}", r.source) for r in rows}
    rule_owner: dict[str, str] = {}
    for sk in parsed.values():
        for rule in sk.meta.detection:
            if rule_owner.setdefault(rule.id, sk.name) != sk.name:
                raise PublishError(f"detection rule {rule.id} is declared by both {rule_owner[rule.id]} and {sk.name}")
    await db.execute(update(SkillGeneration).where(SkillGeneration.status == "active").values(status="retired"))
    gen = SkillGeneration(digest=manifest_digest(manifest), manifest=manifest, status="active", reason=reason[:500], created_by=actor)
    db.add(gen)
    await db.flush()
    await audit.record(db, action="skills.generation", actor_id=actor, resource_type="skill_generation", resource_id=str(gen.generation), decision="allow",
                       details={"digest": gen.digest, "skills": len(manifest), "reason": reason[:300]})
    return gen


async def register(db: AsyncSession, sk: Skill, content: str, others: dict[str, Skill]) -> SkillVersion:
    """Records one exact file. Versions are immutable: reusing a version for different text is refused."""
    row = await db.get(SkillVersion, sk.digest)
    if row is not None:
        return row
    issues = validate(sk, others)
    clash = (await db.execute(select(SkillVersion).where(SkillVersion.skill_id == sk.name, SkillVersion.version == sk.meta.version))).scalars().first()
    if clash is not None:
        issues.append({"level": "error", "where": "version", "message": f"version {sk.meta.version} of {sk.name} already exists with different content; bump it"})
    errors = [i for i in issues if i["level"] == "error"]
    meta = {"title": sk.meta.title, "category": sk.meta.category, "detection": [r.id for r in sk.meta.detection]}
    if clash is not None:  # kept out of the table: the unique key is what makes versions immutable
        raise PublishError(issues[-1]["message"])
    row = SkillVersion(digest=sk.digest, skill_id=sk.name, version=sk.meta.version, owner=sk.meta.owner, source=sk.source, content=content, meta=meta,
                       state="draft" if errors else "validated", validation=issues)
    db.add(row)
    await db.flush()
    return row


async def scan(actor: uuid.UUID | None = None, extra_dir: str | None = None) -> dict:
    """Registers what is on disk and publishes new built-ins. Safe to run in every process at once."""
    lib = load(extra_dir)
    result = {"registered": [], "published": [], "waiting": [], "rejected": list(lib.errors), "generation": None}
    async with session_scope() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK})
        active = await _active(db)
        manifest = dict(active.manifest) if active else {}
        known = set((await db.execute(select(SkillVersion.digest))).scalars())
        changed = []
        now = datetime.now(timezone.utc)
        for sk in lib.skills.values():
            new = sk.digest not in known
            try:
                row = await register(db, sk, sk.text, lib.skills)
            except PublishError as exc:
                result["rejected"].append({"path": sk.path, "error": str(exc)})
                continue
            if new:
                result["registered"].append(sk.name)
            if row.state == "draft":
                if new:
                    result["rejected"].append({"path": sk.path, "error": "; ".join(i["message"] for i in row.validation if i["level"] == "error")})
                continue
            if row.source == "builtin" and new:
                row.state, row.published_at = "published", now
                changed.append(sk.name)
                manifest[sk.name] = sk.digest
            elif row.source == "builtin" and sk.name not in manifest:
                row.state = "published"
                manifest[sk.name] = sk.digest
                changed.append(sk.name)
            elif row.source != "builtin" and row.state == "validated":
                result["waiting"].append(sk.name)
        gone = [n for n, d in manifest.items() if n not in lib.skills and (await db.get(SkillVersion, d)).source == "builtin"]
        for name in gone:
            del manifest[name]
        if changed or gone or active is None:
            try:
                gen = await _activate(db, manifest, f"image update: {', '.join(sorted(changed + gone))[:300]}", actor)
                result["generation"], result["published"] = gen.generation, changed
            except PublishError as exc:
                result["rejected"].append({"path": "generation", "error": str(exc)})
    await refresh(force=True)
    return result


async def publish(db: AsyncSession, digest: str, actor: uuid.UUID, note: str = "") -> SkillGeneration:
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK})
    row = await db.get(SkillVersion, digest, with_for_update=True)
    if row is None:
        raise PublishError("unknown skill version")
    if row.state == "draft":
        raise PublishError("this version failed validation and cannot be published")
    active = await _active(db)
    manifest = dict(active.manifest) if active else {}
    if row.source == "custom" and row.skill_id in manifest:
        current = await db.get(SkillVersion, manifest[row.skill_id])
        pol = await db.get(SkillPolicy, row.skill_id)
        if current.source == "builtin" and not (pol and pol.allow_override):
            raise PublishError(f"{row.skill_id} is a built-in; allow overriding it first")
    now = datetime.now(timezone.utc)
    previous = manifest.get(row.skill_id)
    manifest[row.skill_id] = digest
    gen = await _activate(db, manifest, f"publish {row.skill_id} {row.version}: {note}", actor)
    if previous and previous != digest:
        await db.execute(update(SkillVersion).where(SkillVersion.digest == previous).values(state="deprecated"))
    row.state, row.published_by, row.published_at, row.reviewed_by, row.reviewed_at = "published", actor, now, actor, now
    await audit.record(db, action="skills.publish", actor_id=actor, resource_type="skill", resource_id=row.skill_id, decision="allow",
                       details={"version": row.version, "digest": digest, "generation": gen.generation, "replaced": previous, "note": note[:300]})
    return gen


async def rollback(db: AsyncSession, name: str, digest: str, actor: uuid.UUID, note: str = "") -> SkillGeneration:
    """Back to a version that was published before. A new generation, so history keeps both."""
    row = await db.get(SkillVersion, digest)
    if row is None or row.skill_id != name or row.state not in ("published", "deprecated"):
        raise PublishError("only a previously published version of this skill can be restored")
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK})
    active = await _active(db)
    manifest = dict(active.manifest) if active else {}
    previous = manifest.get(name)
    manifest[name] = digest
    gen = await _activate(db, manifest, f"rollback {name} to {row.version}: {note}", actor)
    if previous and previous != digest:
        await db.execute(update(SkillVersion).where(SkillVersion.digest == previous).values(state="deprecated"))
    row.state = "published"
    await audit.record(db, action="skills.rollback", actor_id=actor, resource_type="skill", resource_id=name, decision="allow",
                       details={"to": digest, "from": previous, "generation": gen.generation, "note": note[:300]})
    return gen


async def refresh(force: bool = False) -> SkillLibrary:
    """Follows the active generation. On any failure the library already in memory stays in use."""
    from . import library as current
    global _checked
    now = time.monotonic()
    lib = current()
    if not force and now - _checked < REFRESH_SECONDS:
        return lib
    _checked = now
    try:
        async with session_scope() as db:
            gen = await _active(db)
            if gen is None or gen.generation == lib.generation:
                return lib
            rows = (await db.execute(select(SkillVersion).where(SkillVersion.digest.in_(list(gen.manifest.values()))))).scalars().all()
            number, digest, manifest = gen.generation, gen.digest, dict(gen.manifest)
            texts = {r.digest: (r.content, r.source, r.skill_id, r.version) for r in rows}
        skills: dict[str, Skill] = {}
        for name, d in manifest.items():
            if d not in texts:
                raise ValueError(f"{name}: version {d[:12]} is not in the registry")
            content, source, sid, version = texts[d]
            sk = parse(content, f"registry:{sid}@{version}", source)
            skills[sk.name] = sk
        replace_library(SkillLibrary(skills, [], number, digest))
    except Exception as exc:  # a generation this build cannot run: keep working on the last good one
        log.error("skill generation not applied", extra={"event": "skill_generation_failed", "errorType": type(exc).__name__})
        lib.error = f"{type(exc).__name__}: {exc}"[:300]
    return current()


def snapshot() -> dict:
    from . import library as current

    lib = current()
    return {"generation": lib.generation, "digest": lib.digest, "error": lib.error}


def scan_on_start() -> bool:
    return os.environ.get("MOKS_SKILLS_AUTOSCAN", "1") != "0"
