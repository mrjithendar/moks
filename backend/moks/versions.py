"""Which build each running component is. Reported every few seconds, so a rollout that stalled
halfway shows up rather than hiding behind a healthy API."""
from __future__ import annotations

import socket
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from . import __version__
from .config import get_settings
from .db import session_scope
from .models import ComponentVersion

FRESH_SECONDS = 300


def build() -> dict[str, str]:
    return {"version": __version__, "commit": get_settings().build_commit}


async def report(component: str) -> None:
    from .skills import registry

    b = build()
    if component != "executor":  # the executor runs no skills
        await registry.refresh()
    skill = registry.snapshot() if component != "executor" else {"generation": None, "digest": None, "error": None}
    async with session_scope() as db:
        await db.execute(text("""INSERT INTO component_versions (instance, component, commit_sha, version, seen_at, skill_generation, skill_digest, skill_error) VALUES (:i, :c, :s, :v, now(), :g, :d, :e)
                                 ON CONFLICT (instance) DO UPDATE SET commit_sha = :s, version = :v, seen_at = now(), skill_generation = :g, skill_digest = :d, skill_error = :e"""),
                         {"i": f"{component}:{socket.gethostname()}"[:120], "c": component, "s": b["commit"], "v": b["version"], "g": skill["generation"], "d": skill["digest"], "e": skill["error"]})


async def running(db: AsyncSession) -> dict[str, Any]:
    rows = (await db.execute(select(ComponentVersion).where(ComponentVersion.seen_at > text(f"now() - interval '{FRESH_SECONDS} seconds'"))
                             .order_by(ComponentVersion.component, ComponentVersion.instance))).scalars().all()
    commits = {r.commit_sha for r in rows}
    return {"api": build(), "components": [{"instance": r.instance, "component": r.component, "commit": r.commit_sha, "version": r.version, "skill_generation": r.skill_generation, "skill_digest": r.skill_digest, "skill_error": r.skill_error, "seen_at": r.seen_at.isoformat()} for r in rows],
            "mixed": len(commits) > 1}
