"""Off-cluster backups of the MOKS database.

The CronJob dumps with the PostgreSQL image, then this module encrypts the dump on the client side,
uploads it, reads it back to prove the copy is intact, and applies retention. The bucket only ever
holds ciphertext, so a bucket credential alone reveals nothing.

Encrypted format (self-contained, see scripts/decrypt-backup.py):

    MAGIC | salt (16) | chunks
    chunk = length (4, big endian) | AES-256-GCM ciphertext of up to CHUNK bytes

The file key comes from HKDF over MOKS_BACKUP_ENCRYPTION_KEY and the salt, so it is unique per file.
Each nonce is the chunk counter plus a final-chunk flag, which makes reordering, tampering and
truncation fail on decrypt. Losing the encryption key loses every backup: it must be stored outside
the cluster.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import struct
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("moks.backup")
MAGIC = b"MOKSBAK1"
CHUNK = 1024 * 1024
OVERDUE_FACTOR = 3  # the portal warns after three missed intervals


class BackupError(Exception):
    pass


def load_key(value: str | None = None) -> bytes:
    raw = value if value is not None else os.environ.get("MOKS_BACKUP_ENCRYPTION_KEY", "")
    try:
        key = base64.b64decode(raw, validate=True)
    except ValueError:
        key = b""
    if len(key) != 32:
        raise BackupError("MOKS_BACKUP_ENCRYPTION_KEY must be 32 random bytes, base64 encoded")
    return key


def _file_key(key: bytes, salt: bytes) -> AESGCM:
    return AESGCM(HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"moks-backup-v1").derive(key))


def _nonce(counter: int, last: bool) -> bytes:
    return struct.pack(">QI", counter, 1 if last else 0)


class Sealer:
    """Encrypts data as it arrives, for a source that cannot be read ahead. One chunk stays buffered
    until close(), because only the final chunk carries the end marker."""

    def __init__(self, dst: BinaryIO, key: bytes):
        salt = os.urandom(16)
        self.dst, self.aes, self.counter, self.pending = dst, _file_key(key, salt), 0, bytearray()
        dst.write(MAGIC + salt)

    def _seal(self, part: bytes, last: bool) -> None:
        sealed = self.aes.encrypt(_nonce(self.counter, last), part, MAGIC)
        self.dst.write(struct.pack(">I", len(sealed)) + sealed)
        self.counter += 1

    def feed(self, data: bytes) -> None:
        self.pending.extend(data)
        while len(self.pending) > CHUNK:
            self._seal(bytes(self.pending[:CHUNK]), False)
            del self.pending[:CHUNK]

    def close(self) -> None:
        self._seal(bytes(self.pending), True)


def encrypt_stream(src: BinaryIO, dst: BinaryIO, key: bytes) -> None:
    sealer = Sealer(dst, key)
    for part in iter(lambda: src.read(CHUNK), b""):
        sealer.feed(part)
    sealer.close()


def decrypt_stream(src: BinaryIO, dst: BinaryIO, key: bytes) -> None:
    if src.read(len(MAGIC)) != MAGIC:
        raise BackupError("not a MOKS backup")
    aes = _file_key(key, src.read(16))
    counter, head = 0, src.read(4)
    while head:
        if len(head) != 4:
            raise BackupError("the backup is truncated")
        sealed = src.read(struct.unpack(">I", head)[0])
        following = src.read(4)
        try:
            dst.write(aes.decrypt(_nonce(counter, not following), sealed, MAGIC))
        except InvalidTag:
            raise BackupError("the backup is damaged, truncated, or the key is wrong") from None
        counter, head = counter + 1, following
    if counter == 0:
        raise BackupError("the backup is empty")


@dataclass(frozen=True)
class Retention:
    hourly: int = 48
    daily: int = 30
    monthly: int = 12


def keys_for(prefix: str, at: datetime) -> dict[str, str]:
    p = prefix.strip("/")
    return {"hourly": f"{p}/hourly/{at:%Y-%m-%dT%H%M%SZ}.dump.enc", "daily": f"{p}/daily/{at:%Y-%m-%d}.dump.enc", "monthly": f"{p}/monthly/{at:%Y-%m}.dump.enc"}


def expired(keys: list[str], keep: int) -> list[str]:
    """Names sort by time, so the newest are last."""
    return sorted(keys)[:-keep] if len(keys) > keep else []


class Store:
    def __init__(self, bucket: str, endpoint: str | None = None, region: str | None = None, client: Any = None):
        import boto3

        self.bucket = bucket
        self.s3 = client or boto3.client("s3", endpoint_url=endpoint or None, region_name=region or None)

    def put(self, path: Path, key: str) -> None:
        self.s3.upload_file(str(path), self.bucket, key)

    def copy(self, source: str, key: str) -> None:
        self.s3.copy_object(Bucket=self.bucket, Key=key, CopySource={"Bucket": self.bucket, "Key": source})

    def exists(self, key: str) -> bool:
        return any(k == key for k in self.keys(key))

    def keys(self, prefix: str) -> list[str]:
        out: list[str] = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            out.extend(o["Key"] for o in page.get("Contents", []))
        return out

    def sha256(self, key: str) -> str:
        digest = hashlib.sha256()
        for part in iter(lambda body=self.s3.get_object(Bucket=self.bucket, Key=key)["Body"]: body.read(CHUNK), b""):
            digest.update(part)
        return digest.hexdigest()

    def download(self, key: str, path: Path) -> None:
        self.s3.download_file(self.bucket, key, str(path))

    def delete(self, keys: list[str]) -> None:
        for key in keys:
            self.s3.delete_object(Bucket=self.bucket, Key=key)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for part in iter(lambda: f.read(CHUNK), b""):
            digest.update(part)
    return digest.hexdigest()


def publish(dump: Path, store: Store, prefix: str, key: bytes, manifest: dict[str, Any], at: datetime, retention: Retention) -> dict[str, Any]:
    """Encrypts, uploads, proves the stored copy matches, then promotes and prunes. Retention runs
    last and only after that proof, so a bad run can never remove the good copies before it."""
    if dump.stat().st_size == 0:
        raise BackupError("the dump is empty")
    sealed = dump.with_suffix(".enc")
    with dump.open("rb") as src, sealed.open("wb") as dst:
        encrypt_stream(src, dst, key)
    names = keys_for(prefix, at)
    digest = _sha256(sealed)
    store.put(sealed, names["hourly"])
    if store.sha256(names["hourly"]) != digest:
        raise BackupError("the uploaded copy does not match")
    size = sealed.stat().st_size
    sealed.unlink()
    meta = sealed.with_suffix(".json")
    meta.write_text(json.dumps({**manifest, "sha256": digest, "bytes": size, "taken_at": at.isoformat()}, sort_keys=True))
    store.put(meta, names["hourly"] + ".json")
    for tier in ("daily", "monthly"):
        if not store.exists(names[tier]):
            store.copy(names["hourly"], names[tier])
            store.copy(names["hourly"] + ".json", names[tier] + ".json")
    removed = 0
    p = prefix.strip("/")
    for tier, keep in (("hourly", retention.hourly), ("daily", retention.daily), ("monthly", retention.monthly)):
        objects = [k for k in store.keys(f"{p}/{tier}/") if k.endswith(".dump.enc")]
        for old in expired(objects, keep):
            store.delete([old, old + ".json"])
            removed += 1
    return {"key": names["hourly"], "sha256": digest, "bytes": size, "pruned": removed}


async def table_counts(db: AsyncSession) -> dict[str, int]:
    rows = (await db.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"))).scalars().all()
    return {t: int((await db.execute(text(f'SELECT count(*) FROM "{t}"'))).scalar_one()) for t in rows}


def compare_counts(expected: dict[str, int], restored: dict[str, int]) -> dict[str, Any]:
    """Against the manifest of the same snapshot, so any difference at all is a failed restore."""
    missing = sorted(set(expected) - set(restored))
    extra = sorted(set(restored) - set(expected))
    differ = {t: {"expected": expected[t], "restored": restored[t]} for t in sorted(set(expected) & set(restored)) if expected[t] != restored[t]}
    return {"ok": not missing and not extra and not differ, "tables": len(expected), "missing": missing, "extra": extra, "differ": differ}


def live_drift(source: dict[str, int], restored: dict[str, int]) -> dict[str, int]:
    """How far the live database has moved since. Information only: it says nothing about integrity."""
    return {t: source[t] - restored.get(t, 0) for t in sorted(source) if source[t] != restored.get(t, 0)}


# Each counts rows that break a promise the application makes. All must be zero after a restore.
INVARIANTS = {
    "approvals_have_plans": "SELECT count(*) FROM approvals a LEFT JOIN action_plans p ON p.id = a.plan_id WHERE p.id IS NULL",
    "plans_have_environments": "SELECT count(*) FROM action_plans p LEFT JOIN environments e ON e.id = p.environment_id WHERE e.id IS NULL",
    "plans_have_requesters": "SELECT count(*) FROM action_plans p LEFT JOIN users u ON u.id = p.requested_by WHERE u.id IS NULL",
    "approved_plans_have_approvals": """SELECT count(*) FROM action_plans p WHERE p.requires_approval AND p.status IN ('approved', 'succeeded', 'recovered')
                                        AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.plan_id = p.id AND a.decision = 'approve')""",
    "bindings_have_users": "SELECT count(*) FROM role_bindings b LEFT JOIN users u ON u.id = b.user_id WHERE u.id IS NULL",
    "messages_have_conversations": "SELECT count(*) FROM conversation_messages m LEFT JOIN conversations c ON c.id = m.conversation_id WHERE c.id IS NULL",
    "evidence_has_investigations": "SELECT count(*) FROM evidence e LEFT JOIN investigations i ON i.id = e.investigation_id WHERE i.id IS NULL",
    "chunks_have_documents": "SELECT count(*) FROM chunks c LEFT JOIN documents d ON d.id = c.document_id WHERE d.id IS NULL",
}


async def check_invariants(db: AsyncSession) -> dict[str, int]:
    return {name: n for name, sql in INVARIANTS.items() if (n := int((await db.execute(text(sql))).scalar_one()))}


def assert_isolated(admin_url: str, live_database: str, scratch: str) -> None:
    """A drill may only ever write to a database it created itself."""
    if not scratch.startswith("moks_restore_drill_") or scratch == live_database:
        raise BackupError(f"refusing to restore into {scratch}: drills use a new moks_restore_drill_ database")
    if "/" in admin_url.replace("://", "").rstrip("/") and admin_url.rsplit("/", 1)[-1].split("?")[0] == live_database:
        raise BackupError("the drill's admin connection must not point at the live database")


def pg_restore_command(admin_url: str, database: str, dump: Path) -> tuple[list[str], dict[str, str]]:
    """Connection details go through the environment: a process list must not show a password."""
    url = urlsplit(admin_url)
    env = {**os.environ, "PGHOST": url.hostname or "localhost", "PGPORT": str(url.port or 5432), "PGDATABASE": database}
    if url.username:
        env["PGUSER"] = url.username
    if url.password:
        env["PGPASSWORD"] = url.password
    return ["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error", f"--dbname={database}", str(dump)], env


async def _smoke(db: AsyncSession) -> dict[str, Any]:
    """What the application does first: read accounts and configuration, and prove the audit chain."""
    from . import audit
    from .models import Environment, User
    from .rag import service

    users = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    environments = (await db.execute(select(func.count()).select_from(Environment))).scalar_one()
    chain = await audit.verify_chain(db)
    index = await service.stats(db)
    return {"users": users, "environments": environments, "audit_chain_ok": chain["ok"], "audit_rows": chain.get("checked"), "documents": index["documents"]}


async def restore_drill(store: Store, object_key: str, key: bytes, admin_url: str, live_database: str, workdir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Restores one backup into a new, isolated database, proves it, then drops that database.
    Never touches the live database: it reads the bucket and creates a database of its own."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    now = now or datetime.now(timezone.utc)
    scratch = f"moks_restore_drill_{os.urandom(4).hex()}"
    assert_isolated(admin_url, live_database, scratch)
    report: dict[str, Any] = {"object": object_key, "database": scratch, "ok": False, "failures": [], "seconds": {}}
    fail = report["failures"].append
    started = time.monotonic()

    def lap(name: str, since: float) -> float:
        at = time.monotonic()
        report["seconds"][name] = round(at - since, 3)
        return at

    sealed, plain, meta = workdir / "drill.enc", workdir / "drill.dump", workdir / "drill.json"
    t = started
    store.download(object_key + ".json", meta)
    manifest = json.loads(meta.read_text())
    store.download(object_key, sealed)
    if _sha256(sealed) != manifest.get("sha256"):
        fail("checksum: the stored copy does not match its manifest")
        return report
    t = lap("download", t)
    try:
        with sealed.open("rb") as src, plain.open("wb") as dst:
            decrypt_stream(src, dst, key)
    except BackupError as exc:
        fail(f"decryption: {exc}")
        return report
    t = lap("decrypt", t)
    base = admin_url.rsplit("/", 1)[0]
    import asyncpg

    admin = await asyncpg.connect(admin_url)
    await admin.execute(f'CREATE DATABASE "{scratch}"')
    await admin.close()
    try:
        args, env = pg_restore_command(admin_url, scratch, plain)
        done = subprocess.run(args, capture_output=True, text=True, env=env)
        if done.returncode:
            fail(f"restore: {done.stderr[:400]}")
            return report
        t = lap("restore", t)
        engine = create_async_engine(f"{base.replace('postgresql://', 'postgresql+asyncpg://')}/{scratch}")
        try:
            async with async_sessionmaker(engine)() as db:
                restored = await table_counts(db)
                cmp = compare_counts(manifest.get("tables") or {}, restored)
                report["counts"] = cmp
                if not cmp["ok"]:
                    fail(f"counts: missing {cmp['missing']}, extra {cmp['extra']}, different {sorted(cmp['differ'])}")
                migration = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
                report["migration"] = migration
                if migration != manifest.get("migration"):
                    fail(f"schema: restored {migration}, manifest says {manifest.get('migration')}")
                broken = await check_invariants(db)
                report["invariants"] = broken
                if broken:
                    fail(f"invariants: {broken}")
                report["smoke"] = await _smoke(db)
                if not report["smoke"]["audit_chain_ok"]:
                    fail("smoke: the audit chain does not verify")
        finally:
            await engine.dispose()
        lap("verify", t)
    finally:
        admin = await asyncpg.connect(admin_url)
        await admin.execute(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
        await admin.close()
    taken = manifest.get("snapshot_at") or manifest.get("taken_at")
    report["recovery_point_seconds"] = round((now - datetime.fromisoformat(taken)).total_seconds()) if taken else None
    report["recovery_time_seconds"] = round(time.monotonic() - started, 3)
    report["ok"] = not report["failures"]
    return report


async def status(db: AsyncSession, interval_minutes: int = 60) -> dict[str, Any]:
    from .models import BackupRun

    rows = (await db.execute(select(BackupRun).order_by(BackupRun.started_at.desc()).limit(10))).scalars().all()
    good = (await db.execute(select(BackupRun).where(BackupRun.status == "success").order_by(BackupRun.started_at.desc()).limit(1))).scalars().first()
    now = datetime.now(timezone.utc)
    overdue = good is None or now - good.finished_at > timedelta(minutes=interval_minutes * OVERDUE_FACTOR)
    return {"last_success_at": good.finished_at.isoformat() if good else None, "overdue": overdue,
            "runs": [{"started_at": r.started_at.isoformat(), "finished_at": r.finished_at.isoformat() if r.finished_at else None, "status": r.status, "key": r.object_key,
                      "bytes": r.size_bytes, "error": r.error} for r in rows]}
