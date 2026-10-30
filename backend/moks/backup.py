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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import select, text
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


def encrypt_stream(src: BinaryIO, dst: BinaryIO, key: bytes) -> None:
    salt = os.urandom(16)
    aes = _file_key(key, salt)
    dst.write(MAGIC + salt)
    current, counter = src.read(CHUNK), 0
    while True:
        following = src.read(CHUNK)
        last = not following
        sealed = aes.encrypt(_nonce(counter, last), current, MAGIC)
        dst.write(struct.pack(">I", len(sealed)) + sealed)
        if last:
            return
        current, counter = following, counter + 1


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


def compare_counts(source: dict[str, int], restored: dict[str, int]) -> dict[str, Any]:
    """A restore may lag a live source, but it must hold every table and never more rows."""
    missing = sorted(set(source) - set(restored))
    extra = sorted(set(restored) - set(source))
    differ = {t: {"source": source[t], "restored": restored[t]} for t in sorted(set(source) & set(restored)) if source[t] != restored[t]}
    bad = [t for t, v in differ.items() if v["restored"] > v["source"]]
    return {"ok": not missing and not extra and not bad, "tables": len(source), "missing": missing, "extra": extra, "differ": differ, "more_than_source": bad}


async def status(db: AsyncSession, interval_minutes: int = 60) -> dict[str, Any]:
    from .models import BackupRun

    rows = (await db.execute(select(BackupRun).order_by(BackupRun.started_at.desc()).limit(10))).scalars().all()
    good = (await db.execute(select(BackupRun).where(BackupRun.status == "success").order_by(BackupRun.started_at.desc()).limit(1))).scalars().first()
    now = datetime.now(timezone.utc)
    overdue = good is None or now - good.finished_at > timedelta(minutes=interval_minutes * OVERDUE_FACTOR)
    return {"last_success_at": good.finished_at.isoformat() if good else None, "overdue": overdue,
            "runs": [{"started_at": r.started_at.isoformat(), "finished_at": r.finished_at.isoformat() if r.finished_at else None, "status": r.status, "key": r.object_key,
                      "bytes": r.size_bytes, "error": r.error} for r in rows]}
