"""CloudNativePG clusters and their backups, read-only.

The operator takes these backups itself (base backups and continuous WAL archiving to an object store), so
MOKS only reports on them: which clusters exist, whether WAL archiving works, what the last base backup was,
how far back a point-in-time recovery can reach, and which schedules cover them. Nothing here writes to a
cluster and nothing here reads a Secret: the rows hold names, phases, timestamps and the destination path.

Field names are those of postgresql.cnpg.io/v1. Every read is defensive, because a cluster backed up through
the Barman Cloud plugin reports through `spec.plugins`, and an in-tree one through `spec.backup`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from .connectors.base import ConnectorError

GROUP = "postgresql.cnpg.io"
VERSION = "v1"
PLURALS = ("clusters", "backups", "scheduledbackups")
STALE_HOURS = 26
MAX_BACKUPS = 200
DONE = "completed"
FAILED = ("failed", "walArchivingFailing")


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
    except ValueError:
        return None


def _key(obj: dict) -> str:
    meta = obj.get("metadata") or {}
    return f"{meta.get('namespace', '')}/{meta.get('name', '')}"


def _condition(status: dict, kind: str) -> dict:
    return next((c for c in status.get("conditions") or [] if c.get("type") == kind), {})


def _archiver(spec: dict) -> dict[str, Any]:
    """How this cluster is backed up, from its spec alone."""
    store = (spec.get("backup") or {}).get("barmanObjectStore") or {}
    if store:
        return {"method": "barmanObjectStore", "destination": store.get("destinationPath"), "retention": (spec.get("backup") or {}).get("retentionPolicy")}
    plugin = next((p for p in spec.get("plugins") or [] if p.get("isWALArchiver") and p.get("enabled", True)), None)
    if plugin:
        return {"method": "plugin", "destination": (plugin.get("parameters") or {}).get("barmanObjectName"), "retention": None, "plugin": plugin.get("name")}
    if (spec.get("backup") or {}).get("volumeSnapshot"):
        return {"method": "volumeSnapshot", "destination": None, "retention": (spec.get("backup") or {}).get("retentionPolicy")}
    return {"method": None, "destination": None, "retention": None}


def cluster_row(obj: dict) -> dict[str, Any]:
    meta, spec, status = obj.get("metadata") or {}, obj.get("spec") or {}, obj.get("status") or {}
    archiving = _condition(status, "ContinuousArchiving")
    how = _archiver(spec)
    return {
        "namespace": meta.get("namespace"), "name": meta.get("name"), "uid": meta.get("uid"),
        "phase": status.get("phase") or "Unknown", "healthy": status.get("phase") == "Cluster in healthy state",
        "instances": spec.get("instances"), "ready": status.get("readyInstances", 0), "primary": status.get("currentPrimary"),
        "image": spec.get("imageName") or status.get("image"), "storage": (spec.get("storage") or {}).get("size"),
        "backup_method": how["method"], "destination": how["destination"], "retention": how["retention"], "plugin": how.get("plugin"),
        # None: the operator has not reported on archiving, which is also what an unconfigured cluster looks like.
        "archiving": None if not archiving else archiving.get("status") == "True",
        "archiving_message": archiving.get("message") if archiving.get("status") != "True" else None,
        "first_recoverability_point": status.get("firstRecoverabilityPoint"),
        "last_successful_backup": status.get("lastSuccessfulBackup"), "last_failed_backup": status.get("lastFailedBackup"),
    }


def backup_row(obj: dict) -> dict[str, Any]:
    meta, spec, status = obj.get("metadata") or {}, obj.get("spec") or {}, obj.get("status") or {}
    started, stopped = _parse(status.get("startedAt")), _parse(status.get("stoppedAt"))
    schedule = (meta.get("labels") or {}).get("cnpg.io/scheduled-backup") or next(
        (o.get("name") for o in meta.get("ownerReferences") or [] if o.get("kind") == "ScheduledBackup"), None)
    return {
        "namespace": meta.get("namespace"), "name": meta.get("name"), "cluster": (spec.get("cluster") or {}).get("name"),
        "phase": status.get("phase") or "pending", "method": status.get("method") or spec.get("method") or "barmanObjectStore",
        "started": status.get("startedAt") or meta.get("creationTimestamp"), "stopped": status.get("stoppedAt"),
        "duration_seconds": int((stopped - started).total_seconds()) if started and stopped else None,
        "schedule": schedule, "backup_id": status.get("backupId"), "destination": status.get("destinationPath"),
        "begin_wal": status.get("beginWal"), "end_wal": status.get("endWal"), "error": (status.get("error") or None) and str(status["error"])[:500],
    }


def schedule_row(obj: dict) -> dict[str, Any]:
    meta, spec, status = obj.get("metadata") or {}, obj.get("spec") or {}, obj.get("status") or {}
    return {"namespace": meta.get("namespace"), "name": meta.get("name"), "cluster": (spec.get("cluster") or {}).get("name"),
            "cron": spec.get("schedule"), "suspended": bool(spec.get("suspend")), "method": spec.get("method") or "barmanObjectStore",
            "last_run": status.get("lastScheduleTime"), "next_run": status.get("nextScheduleTime")}


def findings(state: dict[str, Any], now: datetime | None = None) -> list[dict[str, str]]:
    """Deterministic checks, in the shape the Backups page already shows for Velero and database dumps."""
    now = now or datetime.now(timezone.utc)
    out: list[dict[str, str]] = []
    add = lambda severity, code, text: out.append({"severity": severity, "code": code, "text": text})  # noqa: E731
    if not state["installed"]:
        return out  # absence is reported once by the page, not as a problem: most clusters do not run the operator
    for c in state["clusters"]:
        who = f"{c['namespace']}/{c['name']}"
        if not c["backup_method"]:
            add("bad", f"cnpg_unconfigured:{who}", f"PostgreSQL cluster {who} has no backup configured, so it cannot be restored from the operator's backups.")
            continue
        if c["archiving"] is False:
            add("bad", f"cnpg_archiving:{who}", f"WAL archiving is failing for {who}" + (f": {c['archiving_message']}" if c.get("archiving_message") else ".")
                + " Point-in-time recovery stops at the last archived segment.")
        mine = [b for b in state["backups"] if (b["namespace"], b["cluster"]) == (c["namespace"], c["name"])]
        good = [_parse(b["stopped"]) for b in mine if b["phase"] == DONE]
        last = max([t for t in [*good, _parse(c["last_successful_backup"])] if t], default=None)
        if last is None:
            add("bad", f"cnpg_no_backup:{who}", f"{who} has no completed base backup.")
        elif (now - last).total_seconds() / 3600 > STALE_HOURS:
            add("warn", f"cnpg_stale:{who}", f"The last completed base backup of {who} is {(now - last).total_seconds() / 3600:.0f} hours old.")
        if mine and mine[0]["phase"] in FAILED:
            add("warn", f"cnpg_last_failed:{who}", f"The most recent backup of {who} ({mine[0]['name']}) failed" + (f": {mine[0]['error']}" if mine[0].get("error") else "."))
        plans = [s for s in state["schedules"] if (s["namespace"], s["cluster"]) == (c["namespace"], c["name"])]
        if not plans:
            add("warn", f"cnpg_unscheduled:{who}", f"No ScheduledBackup covers {who}, so base backups only happen when someone creates one.")
        elif all(s["suspended"] for s in plans):
            add("warn", f"cnpg_suspended:{who}", f"Every ScheduledBackup of {who} is suspended.")
    return out


async def status(k8s: Any) -> dict[str, Any]:
    state: dict[str, Any] = {"installed": True, "clusters": [], "backups": [], "schedules": [], "truncated": False}
    try:
        clusters, backups, schedules = await asyncio.gather(*(k8s.cnpg_list(p) for p in PLURALS))
    except ConnectorError as exc:
        if exc.kind not in ("not_found", "forbidden"):
            raise
        state["installed"] = False
        state["error"] = ("CloudNativePG is not installed in this cluster." if exc.kind == "not_found"
                          else "MOKS has no read access to CloudNativePG here (set cnpg.enabled in the chart).")
    else:
        newest = lambda row: row.get("started") or ""  # noqa: E731
        rows = sorted((backup_row(b) for b in backups), key=newest, reverse=True)
        state["clusters"] = sorted((cluster_row(c) for c in clusters), key=lambda c: (c["namespace"] or "", c["name"] or ""))
        state["backups"], state["truncated"] = rows[:MAX_BACKUPS], len(rows) > MAX_BACKUPS
        state["schedules"] = sorted((schedule_row(s) for s in schedules), key=lambda s: (s["namespace"] or "", s["name"] or ""))
        state["findings"] = findings({**state, "backups": rows})
        return state
    state["findings"] = findings(state)
    return state
