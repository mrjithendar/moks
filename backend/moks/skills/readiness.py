"""Whether a skill can do its job in one environment. Four answers:

  ready       every requirement was checked and holds
  partial     it can run, but some steps cannot
  unavailable it cannot do its job here
  unknown     nothing is known to be missing, but something could not be checked

Unknown is not a pass. Telemetry nobody could read is reported as unknown, never as present.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from . import Skill

log = logging.getLogger("moks.skills")
METRIC_LIMIT = 20000
TTL = 300
_metrics: dict[str, tuple[float, set[str] | None]] = {}


async def metric_names(key: str, prom: Any) -> tuple[set[str] | None, bool]:
    """(names or None, complete). A capped list can show a metric exists, never that one does not."""
    now = time.monotonic()
    cached = _metrics.get(key)
    if cached and now - cached[0] < TTL:
        return cached[1], cached[1] is not None and len(cached[1]) < METRIC_LIMIT
    end = datetime.now(timezone.utc)
    try:
        names = set(await asyncio.wait_for(prom.label_values("__name__", None, end - timedelta(hours=1), end, METRIC_LIMIT), 5))
    except Exception as exc:
        log.info("metric list unavailable", extra={"event": "readiness_unknown", "errorType": type(exc).__name__})
        names = None
    _metrics[key] = (now, names)
    return names, names is not None and len(names) < METRIC_LIMIT


def readiness(sk: "Skill", connectors: set[str], metrics: set[str] | None = None, metrics_complete: bool = True, can=None, resources: set[str] | None = None) -> dict:
    """`can(permission)` answers for the person or automation that would run it. None: not checked.
    `resources` are the kinds this run is about: a skill for something else can still run, but only in part.
    A metric counts as present only if it reported in the last hour, so stale telemetry reads as missing."""
    from ..tools.builtin import REGISTRY
    from ..tools.contracts import ANY_OF_CONNECTORS, PSEUDO_CONNECTORS

    checks: list[dict[str, str]] = []

    def add(kind: str, name: str, status: str, required: bool) -> None:
        checks.append({"kind": kind, "name": name, "status": status, "required": required})

    have = connectors | set(PSEUDO_CONNECTORS) | {k for k, v in ANY_OF_CONNECTORS.items() if any(c in connectors for c in v)}
    for c in sorted(sk.meta.requires):
        add("connector", c, "ok" if c in have else "missing", True)
    runnable = 0
    for s in sk.meta.steps:
        spec = REGISTRY[s.tool]
        absent = [c for c in (spec.connector_type, *spec.also_requires) if c not in have]
        runnable += not absent
        add("step", absent[0] if absent else s.tool, "missing" if absent else "ok", s.required)
    for m in sk.meta.telemetry:
        if metrics is None:
            add("telemetry", m, "unknown", True)
        elif m in metrics:
            add("telemetry", m, "ok", True)
        else:
            add("telemetry", m, "missing" if metrics_complete else "unknown", True)
    for p in sk.meta.permissions:
        add("permission", p, "unknown" if can is None else "ok" if can(p) else "missing", True)
    if resources and sk.meta.resource_types:
        for kind in sorted(set(sk.meta.resource_types) & resources) or sorted(resources)[:1]:
            add("resource", kind, "ok" if kind in sk.meta.resource_types else "missing", False)
    blocking = [c for c in checks if c["status"] == "missing" and c["required"]]
    missing = [c for c in checks if c["status"] == "missing"]
    if blocking or (sk.meta.steps and not runnable):
        state = "unavailable"
    elif missing:
        state = "partial"
    elif any(c["status"] == "unknown" for c in checks):
        state = "unknown"
    else:
        state = "ready"
    word = {"connector": "needs the {} connector", "step": "needs the {} connector", "telemetry": "needs metric {} reported in the last hour", "permission": "needs permission {}", "resource": "is not written for {}"}
    fix = {"connector": "Add a {} connector to this environment under Connectors.", "step": "Add a {} connector to this environment under Connectors.", "telemetry": "Check that {} is being scraped here; it has not reported in the last hour.",
           "permission": "Ask an administrator for a role that includes {}.", "resource": "Expect partial results for {}: this skill does not list it."}
    reasons = [word[c["kind"]].format(c["name"]) for c in missing] + [f"could not check {c['name']}" for c in checks if c["status"] == "unknown"]
    actions = list(dict.fromkeys(fix[c["kind"]].format(c["name"]) for c in missing))
    return {"state": state, "available": state != "unavailable", "reasons": list(dict.fromkeys(reasons)), "actions": actions, "checks": checks}
