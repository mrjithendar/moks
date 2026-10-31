"""Database backups for clusters where Velero cannot copy volumes.

Same store and same permissions model as cluster backups, one layer down. A plan queues a run; the
executor runs the database's own dump tool inside its pod and reads the dump over the exec stream.
The login stays in the pod (local socket, or the pod's own variables): MOKS never sees it and copies
no key into the namespace. The stream is encrypted as it arrives (see backup.py), uploaded next to
the Velero data, read back to prove the copy, and described by a manifest.

Dumps go under <prefix>/database-backups/, not <prefix>/<env>/, because Velero marks a location
unavailable when it finds folders of its own prefix it does not know. Nothing here deletes a
backup: history is kept until someone removes it from the bucket.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from sqlalchemy import select

from . import audit, backup, velero
from .config import get_settings
from .connectors.base import ConnectorError, resolve_credential
from .db import session_scope
from .models import DatabaseBackup, DatabaseBackupSchedule, Environment
from .redaction import redact

log = logging.getLogger("moks.dbbackup")
STALE_HOURS = 26
MAX_INVENTORY = 4_000_000
PG_LOGIN = '-U "$POSTGRES_USER" -d "${POSTGRES_DB:-$POSTGRES_USER}"'
PG_INVENTORY = """SELECT json_build_object(
    'version', current_setting('server_version'), 'database', current_database(), 'bytes', pg_database_size(current_database()),
    'other_databases', (SELECT coalesce(json_agg(datname ORDER BY datname), '[]') FROM pg_database
                        WHERE NOT datistemplate AND datname NOT IN (current_database(), 'postgres')),
    'tables', (SELECT coalesce(json_agg(t), '[]') FROM (
        SELECT schemaname AS schema, relname AS name, n_live_tup AS rows, pg_total_relation_size(relid) AS bytes
        FROM pg_stat_user_tables ORDER BY schemaname, relname) t))"""
MONGO_LOGIN = ('${MONGO_INITDB_ROOT_USERNAME:+--username "$MONGO_INITDB_ROOT_USERNAME" --password "$MONGO_INITDB_ROOT_PASSWORD" '
               '--authenticationDatabase admin}')
MONGO_INVENTORY = """const out = {version: db.version(), databases: []};
for (const d of db.adminCommand({listDatabases: 1}).databases) {
  if (['admin', 'local', 'config'].includes(d.name)) continue;
  const s = db.getSiblingDB(d.name);
  out.databases.push({name: d.name, bytes: Number(d.sizeOnDisk), tables: s.getCollectionInfos({type: 'collection'}).map(
    (c) => ({name: c.name, rows: Number(s.getCollection(c.name).estimatedDocumentCount())}))});
}
print(JSON.stringify(out));"""


@dataclass(frozen=True)
class Engine:
    name: str
    tool: str
    dump: list[str]
    inventory: list[str]
    magic: bytes
    suffix: str
    restore: str


ENGINES = {
    "postgresql": Engine("postgresql", "pg_dump", ["sh", "-c", f"exec pg_dump -Fc {PG_LOGIN}"],
                         ["sh", "-c", f'exec psql -AtX {PG_LOGIN} -c "$1"', "inventory", PG_INVENTORY], b"PGDMP", "dump",
                         "scripts/decrypt-backup.py, then pg_restore --no-owner --dbname=<database> <file>"),
    "mongodb": Engine("mongodb", "mongodump", ["sh", "-c", f"exec mongodump --archive --gzip {MONGO_LOGIN}"],
                      ["sh", "-c", f'exec mongosh --quiet {MONGO_LOGIN} --eval "$1"', "inventory", MONGO_INVENTORY], b"\x1f\x8b", "archive.gz",
                      "scripts/decrypt-backup.py, then mongorestore --archive=<file> --gzip"),
}
IMAGES = {"postgres": "postgresql", "pgvector": "postgresql", "postgis": "postgresql", "timescaledb": "postgresql", "mongo": "mongodb", "mongodb": "mongodb"}
NOT_COVERED = {"redis", "valkey", "rabbitmq", "mysql", "mariadb", "kafka", "elasticsearch", "opensearch"}


def repository(image: str) -> str:
    return image.split("@")[0].rsplit("/", 1)[-1].split(":")[0]


def engine_of(image: str) -> str | None:
    return IMAGES.get(repository(image))


def allowed_namespaces() -> set[str]:
    return {n for n in re.split(r"[\s,]+", get_settings().database_backup_namespaces) if n}


def in_policy(env: Environment, ns: str) -> bool:
    allowed = env.allowed_namespaces or ["*"]
    return any(fnmatchcase(ns, a) for a in allowed) and not any(fnmatchcase(ns, d) for d in env.denied_namespaces or [])


async def discover(k8s: Any, env: Environment) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Databases MOKS can back up, and stateful services it cannot, so a gap is visible."""
    enabled = allowed_namespaces()
    found, skipped = [], []
    for ns in sorted(n for n in await k8s.list_namespaces() if in_policy(env, n)):
        for w in await k8s.workloads(ns):
            if w["kind"] not in ("Deployment", "StatefulSet"):
                continue
            images = w.get("images") or []
            engine = next((e for e in map(engine_of, images) if e), None)
            row = {"namespace": ns, "workload": w["name"], "kind": w["kind"], "ready": w.get("ready", 0)}
            if engine:
                found.append({**row, "engine": engine, "image": next(i for i in images if engine_of(i)), "enabled": ns in enabled})
            else:
                skipped += [{**row, "service": r} for r in {repository(i) for i in images} & NOT_COVERED]
    return found, skipped


def object_key(store: Any, env_name: str, ns: str, workload: str, engine: str, at: datetime) -> str:
    parts = (store.prefix.strip("/"), "database-backups", env_name, ns, workload)
    return "/".join(p for p in parts if p) + f"/{at:%Y-%m-%dT%H%M%SZ}.{ENGINES[engine].suffix}.enc"


def inventory_of(engine: str, text: str) -> dict[str, Any]:
    """The same shape for every engine. Row counts are the server's estimates, not a count."""
    data, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])  # psql splits arrays across lines; tools may print a notice first
    if engine == "postgresql":
        databases = [{"name": data["database"], "bytes": data["bytes"], "tables": data["tables"]}]
        version, others = data["version"], data["other_databases"]
    else:
        databases, version, others = data["databases"], data["version"], []
    return {"version": version, "databases": databases, "not_included": others, "table_count": sum(len(d["tables"]) for d in databases),
            "row_estimate": sum(max(int(t.get("rows") or 0), 0) for d in databases for t in d["tables"])}


def row_out(r: DatabaseBackup) -> dict[str, Any]:
    return {"id": str(r.id), "namespace": r.namespace, "workload": r.workload, "kind": r.workload_kind, "engine": r.engine, "status": r.status,
            "trigger": r.trigger, "created_at": r.created_at.isoformat() if r.created_at else None, "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None, "object_key": r.object_key, "bytes": r.size_bytes, "dump_bytes": r.dump_bytes,
            "sha256": r.sha256, "image": r.image, "pod": r.pod, "inventory": r.inventory, "error": r.error, "plan_id": str(r.plan_id) if r.plan_id else None}


def findings(targets: list[dict], backups: list[dict], schedules: list[dict], now: datetime | None = None) -> list[dict[str, str]]:
    now = now or datetime.now(timezone.utc)
    out = []
    for t in targets:
        name = f"{t['namespace']}/{t['workload']}"
        if not t["enabled"]:
            out.append({"severity": "warn", "code": f"not_enabled:{name}", "text": f"{name} cannot be backed up: add {t['namespace']} to databaseBackups.namespaces."})
            continue
        good = [b for b in backups if (b["namespace"], b["workload"]) == (t["namespace"], t["workload"]) and b["status"] == "success"]
        last = max((b["finished_at"] for b in good), default=None)
        if last is None:
            out.append({"severity": "bad", "code": f"never:{name}", "text": f"{name} has no database backup."})
        elif now - datetime.fromisoformat(last) > timedelta(hours=STALE_HOURS):
            out.append({"severity": "warn", "code": f"stale:{name}", "text": f"The last backup of {name} is over {STALE_HOURS} hours old."})
        if not any((s["namespace"], s["workload"]) == (t["namespace"], t["workload"]) and not s["paused"] for s in schedules):
            out.append({"severity": "warn", "code": f"no_schedule:{name}", "text": f"{name} has no active backup schedule."})
    for b in backups:
        missing = (b.get("inventory") or {}).get("not_included") or []
        if missing and b["status"] == "success":
            out.append({"severity": "warn", "code": f"partial:{b['namespace']}/{b['workload']}",
                        "text": f"{b['namespace']}/{b['workload']} also holds {', '.join(missing)}, which this backup does not include."})
            break
    return out


async def _take(row: dict[str, Any], store: Any, writer: Any) -> dict[str, Any]:
    spec = ENGINES[row["engine"]]
    pod = await writer.database_pod(row["namespace"], row["workload_kind"], row["workload"])
    if engine_of(pod["image"]) != spec.name:
        raise backup.BackupError(f"{row['workload']} now runs {pod['image']}, not {spec.name}")
    timeout = get_settings().database_backup_timeout_seconds
    listing = bytearray()

    async def collect(part: bytes) -> None:
        if len(listing) < MAX_INVENTORY:
            listing.extend(part)
    code, err = await writer.exec_stream(row["namespace"], pod["name"], pod["container"], spec.inventory, collect, 60)
    try:
        if code != 0:
            raise ValueError(redact(err)[:300])
        inventory = inventory_of(spec.name, listing.decode())
    except (ValueError, KeyError, TypeError) as exc:  # the dump is what matters; say what is unknown
        inventory = {"error": f"could not list the contents: {str(exc)[:300]}"}
    key = backup.load_key(resolve_credential((store.credentials or {}).get("sse_key")) or "")
    work = Path(get_settings().database_backup_dir)
    sealed = work / f"{row['id']}.enc"
    size = 0
    head = bytearray()
    try:
        with sealed.open("wb") as dst:
            sealer = backup.Sealer(dst, key)

            async def seal(part: bytes) -> None:
                nonlocal size
                if len(head) < len(spec.magic):
                    head.extend(part[: len(spec.magic) - len(head)])
                size += len(part)
                sealer.feed(part)
            code, err = await writer.exec_stream(row["namespace"], pod["name"], pod["container"], spec.dump, seal, timeout)
            sealer.close()
        if code != 0:  # a missing exit status counts: a cut stream must not pass as a backup
            raise backup.BackupError(f"{spec.tool} failed (exit {code}): {redact(err)[:400]}")
        if bytes(head) != spec.magic:
            raise backup.BackupError(f"{spec.tool} did not produce a {spec.name} dump")
        now = datetime.now(timezone.utc)
        name = object_key(store, row["env_name"], row["namespace"], row["workload"], spec.name, now)
        digest = backup._sha256(sealed)
        stored = sealed.stat().st_size
        manifest = {"format": "moks-database-1", "engine": spec.name, "environment": row["env_name"], "namespace": row["namespace"], "workload": row["workload"],
                    "kind": row["workload_kind"], "pod": pod["name"], "image": pod["image"], "taken_at": now.isoformat(), "tool": spec.tool, "dump_bytes": size,
                    "bytes": stored, "sha256": digest, "encryption": "MOKSBAK1 (AES-256-GCM)", "restore": spec.restore, "inventory": inventory}

        def upload() -> None:
            target = backup.Store(store.bucket, client=velero._s3_client(store))
            target.put(sealed, name)
            if target.sha256(name) != digest:
                raise backup.BackupError("the uploaded copy does not match")
            meta = sealed.with_suffix(".json")
            meta.write_text(json.dumps(manifest, sort_keys=True))
            target.put(meta, name + ".json")
            meta.unlink()
        await asyncio.to_thread(upload)
    finally:
        sealed.unlink(missing_ok=True)
    return {"object_key": name, "size_bytes": stored, "dump_bytes": size, "sha256": digest, "image": pod["image"], "pod": pod["name"], "inventory": inventory}


async def run(backup_id: uuid.UUID, writer: Any) -> None:
    async with session_scope() as db:
        r = await db.get(DatabaseBackup, backup_id)
        env = await db.get(Environment, r.environment_id)
        store = await velero.get_store(db)
        row = {"id": r.id, "namespace": r.namespace, "workload": r.workload, "workload_kind": r.workload_kind, "engine": r.engine, "env_name": env.name}
    out, error = {}, None
    try:
        if store is None:
            raise backup.BackupError("the R2 settings are missing")
        out = await _take(row, store, writer)
    except (ConnectorError, backup.BackupError) as exc:
        error = str(exc)
    except OSError as exc:
        error = f"could not write the backup locally ({exc.strerror or type(exc).__name__})"
    except Exception as exc:
        log.exception("database backup failed", extra={"event": "database_backup_failed"})
        error = f"internal error ({type(exc).__name__})"
    async with session_scope() as db:
        r = await db.get(DatabaseBackup, backup_id)
        r.status, r.error, r.finished_at = ("failed" if error else "success"), error, datetime.now(timezone.utc)
        for k, v in out.items():
            setattr(r, k, v)
        await audit.record(db, action="database_backup.run", resource_type="database_backup", resource_id=str(backup_id), environment=row["env_name"],
                           namespace=row["namespace"], decision="allow", outcome="failed" if error else "success",
                           details={"workload": row["workload"], "engine": row["engine"], "trigger": r.trigger, "error": error, "object_key": out.get("object_key"),
                                    "sha256": out.get("sha256"), "bytes": out.get("size_bytes")})


_task: asyncio.Task | None = None


async def _queue_due(db: Any, now: datetime) -> None:
    due = (await db.execute(select(DatabaseBackupSchedule).where(DatabaseBackupSchedule.paused.is_(False), DatabaseBackupSchedule.next_run_at <= now)
                            .with_for_update(skip_locked=True))).scalars().all()
    for s in due:
        s.next_run_at = now + timedelta(hours=s.interval_hours)
        busy = (await db.execute(select(DatabaseBackup.id).where(DatabaseBackup.environment_id == s.environment_id, DatabaseBackup.namespace == s.namespace,
                                                                 DatabaseBackup.workload == s.workload, DatabaseBackup.status.in_(["queued", "running"])).limit(1))).first()
        if not busy:
            db.add(DatabaseBackup(environment_id=s.environment_id, namespace=s.namespace, workload=s.workload, workload_kind=s.workload_kind, engine=s.engine,
                                  status="queued", trigger="schedule"))


async def tick(writer_override: Any = None, wait: bool = False) -> None:
    """Executor loop: runs one backup at a time beside it, so a long dump never holds up an action."""
    global _task
    from .actions.executor import writer_factory

    if _task is not None and not _task.done():
        return
    now = datetime.now(timezone.utc)
    limit = now - timedelta(seconds=get_settings().database_backup_timeout_seconds + 120)
    async with session_scope() as db:
        stale = (await db.execute(select(DatabaseBackup).where(DatabaseBackup.status == "running", DatabaseBackup.started_at < limit))).scalars().all()
        for r in stale:
            r.status, r.error, r.finished_at = "failed", "the executor stopped before this finished", now
        await _queue_due(db, now)
        next_run = (await db.execute(select(DatabaseBackup).where(DatabaseBackup.status == "queued").order_by(DatabaseBackup.created_at)
                                     .with_for_update(skip_locked=True).limit(1))).scalars().first()
        if next_run is None:
            return
        next_run.status, next_run.started_at = "running", now
        backup_id = next_run.id
        env = await db.get(Environment, next_run.environment_id)

    async def work() -> None:
        writer = writer_override or await writer_factory(env)
        try:
            await run(backup_id, writer)
        finally:
            if writer_override is None:
                await writer.aclose()
    _task = asyncio.create_task(work())
    if wait:
        await _task
