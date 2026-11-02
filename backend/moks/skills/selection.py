"""Which skills may run, decided before any ranking. Production and evaluation both call this, so an evaluation
measures what investigations actually do, not a broader or friendlier version of it."""
from __future__ import annotations

from typing import Callable

from . import Skill
from .readiness import readiness
from .registry import Policy, allowed


def eligible(skills: dict[str, Skill], environment: str, view: Policy, connectors: set[str], metrics: set[str] | None = None, metrics_complete: bool = False,
             can: Callable[[str], bool] | None = None, resources: set[str] | None = None) -> tuple[dict[str, dict], dict[str, str]]:
    """(skill name to readiness, skill name to why it was refused). Publication, enablement, environment policy,
    connectors, telemetry and permissions all come first: an unavailable skill must not take a place from one that
    can run. Unknown does not exclude, and is reported."""
    ok, refused = {}, {}
    for sk in skills.values():
        permitted, why = allowed(sk, environment, view)
        if not permitted:
            refused[sk.name] = why or "not allowed here"
            continue
        state = readiness(sk, connectors, metrics, metrics_complete, can, resources)
        if state["state"] == "unavailable":
            refused[sk.name] = "; ".join(state["reasons"]) or "unavailable here"
            continue
        ok[sk.name] = state
    return ok, refused
