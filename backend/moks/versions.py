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
    b = build()
    async with session_scope() as db:
        await db.execute(text("""INSERT INTO component_versions (instance, component, commit_sha, version, seen_at) VALUES (:i, :c, :s, :v, now())
                                 ON CONFLICT (instance) DO UPDATE SET commit_sha = :s, version = :v, seen_at = now()"""),
                         {"i": f"{component}:{socket.gethostname()}"[:120], "c": component, "s": b["commit"], "v": b["version"]})


async def running(db: AsyncSession) -> dict[str, Any]:
    rows = (await db.execute(select(ComponentVersion).where(ComponentVersion.seen_at > text(f"now() - interval '{FRESH_SECONDS} seconds'"))
                             .order_by(ComponentVersion.component, ComponentVersion.instance))).scalars().all()
    commits = {r.commit_sha for r in rows}
    return {"api": build(), "components": [{"instance": r.instance, "component": r.component, "commit": r.commit_sha, "version": r.version, "seen_at": r.seen_at.isoformat()} for r in rows],
            "mixed": len(commits) > 1}
