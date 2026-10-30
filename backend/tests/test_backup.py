"""Encryption, retention, overdue detection, and a full dump, encrypt, upload, restore cycle."""
import base64
import hashlib
import io
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest

from moks import audit, backup
from moks.db import session_scope
from moks.models import BackupRun
from tests.conftest import _base, DB_NAME

KEY = bytes(range(32))


class S3:
    """In-memory S3 with the calls Store makes. corrupt=True returns different bytes on read."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.corrupt = False
        self.deleted: list[str] = []

    def upload_file(self, path, bucket, key):
        self.objects[key] = Path(path).read_bytes()

    def copy_object(self, Bucket, Key, CopySource):
        self.objects[Key] = self.objects[CopySource["Key"]]

    def get_paginator(self, name):
        s3 = self

        class P:
            def paginate(self, Bucket, Prefix):
                return [{"Contents": [{"Key": k} for k in sorted(s3.objects) if k.startswith(Prefix)]}]
        return P()

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(b"junk" if self.corrupt else self.objects[Key])}

    def download_file(self, bucket, key, path):
        Path(path).write_bytes(self.objects[key])

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop(Key, None)


def sealed(data: bytes, key: bytes = KEY) -> bytes:
    out = io.BytesIO()
    backup.encrypt_stream(io.BytesIO(data), out, key)
    return out.getvalue()


def opened(data: bytes, key: bytes = KEY) -> bytes:
    out = io.BytesIO()
    backup.decrypt_stream(io.BytesIO(data), out, key)
    return out.getvalue()


@pytest.mark.parametrize("size", [0, 1, backup.CHUNK - 1, backup.CHUNK, backup.CHUNK + 1, backup.CHUNK * 3 + 7])
def test_round_trip_across_chunk_boundaries(size):
    data = os.urandom(size)
    assert opened(sealed(data)) == data


def test_ciphertext_reveals_nothing_and_differs_each_time():
    data = b"password_hash secret " * 100
    one, two = sealed(data), sealed(data)
    assert b"password_hash" not in one and one != two


def test_wrong_key_tampering_and_truncation_are_refused():
    data = os.urandom(backup.CHUNK * 2 + 10)
    enc = sealed(data)
    with pytest.raises(backup.BackupError):
        opened(enc, bytes(32))
    flipped = bytearray(enc)
    flipped[len(enc) // 2] ^= 1
    with pytest.raises(backup.BackupError):
        opened(bytes(flipped))
    for cut in (backup.CHUNK + 100, len(enc) - 1, len(backup.MAGIC) + 16 + 4 + backup.CHUNK + 16):  # the last is a clean chunk boundary
        with pytest.raises(backup.BackupError):
            opened(enc[:cut])
    with pytest.raises(backup.BackupError):
        opened(b"not a backup")


def test_standalone_script_reads_what_moks_writes(tmp_path):
    src, dst = tmp_path / "a.enc", tmp_path / "a.dump"
    data = os.urandom(backup.CHUNK + 5)
    src.write_bytes(sealed(data))
    script = Path(__file__).parents[2] / "scripts" / "decrypt-backup.py"
    subprocess.run([sys.executable, str(script), str(src), str(dst)], check=True, env={**os.environ, "MOKS_BACKUP_ENCRYPTION_KEY": base64.b64encode(KEY).decode()})
    assert dst.read_bytes() == data


@pytest.mark.parametrize("value", ["", "short", "!!!not base64"])
def test_a_bad_key_is_refused_before_anything_happens(value):
    with pytest.raises(backup.BackupError):
        backup.load_key(value)


def _publish(s3, tmp_path, at, retention=backup.Retention(2, 2, 2)):
    dump = tmp_path / "moks.dump"
    dump.write_bytes(b"dump-" + at.isoformat().encode())
    return backup.publish(dump, backup.Store("b", client=s3), "moks/mk4", KEY, {"tables": {"users": 1}}, at, retention)


def test_tiers_are_promoted_once_and_retention_keeps_the_newest(tmp_path):
    s3 = S3()
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    for day in range(5):
        for hour in (0, 12):
            _publish(s3, tmp_path, start + timedelta(days=day, hours=hour))
    names = sorted(k for k in s3.objects if k.endswith(".dump.enc"))
    assert [k.split("/")[-1] for k in names if "/daily/" in k] == ["2026-10-04.dump.enc", "2026-10-05.dump.enc"]
    assert [k.split("/")[-1] for k in names if "/hourly/" in k] == ["2026-10-05T000000Z.dump.enc", "2026-10-05T120000Z.dump.enc"]
    assert [k.split("/")[-1] for k in names if "/monthly/" in k] == ["2026-10.dump.enc"]
    assert all(k + ".json" in s3.objects for k in names)
    first = s3.objects["moks/mk4/monthly/2026-10.dump.enc"]  # the first of the month stays the monthly copy
    assert opened(first).startswith(b"dump-2026-10-01T00:00")


def test_a_failed_upload_check_prunes_nothing(tmp_path):
    s3 = S3()
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    for hour in range(3):
        _publish(s3, tmp_path, start + timedelta(hours=hour))
    before = sorted(s3.objects)
    s3.deleted.clear()
    s3.corrupt = True
    with pytest.raises(backup.BackupError):
        _publish(s3, tmp_path, start + timedelta(hours=9))
    assert not s3.deleted and set(before) <= set(s3.objects)


def test_an_empty_dump_is_never_uploaded(tmp_path):
    s3 = S3()
    dump = tmp_path / "moks.dump"
    dump.write_bytes(b"")
    with pytest.raises(backup.BackupError):
        backup.publish(dump, backup.Store("b", client=s3), "p", KEY, {}, datetime.now(timezone.utc), backup.Retention())
    assert not s3.objects


def test_retention_keeps_everything_under_the_limit():
    assert backup.expired(["a", "b"], 5) == [] and backup.expired(["c", "a", "b"], 2) == ["a"]


def test_row_count_comparison():
    assert backup.compare_counts({"a": 5}, {"a": 5})["ok"]
    assert backup.compare_counts({"a": 6}, {"a": 5})["ok"]  # a live source keeps moving
    assert not backup.compare_counts({"a": 5}, {"a": 6})["ok"]
    assert not backup.compare_counts({"a": 5, "b": 1}, {"a": 5})["ok"]


async def test_overdue_when_nothing_succeeded_and_after_three_intervals():
    async with session_scope() as db:
        await db.execute(BackupRun.__table__.delete())
        assert (await backup.status(db))["overdue"]
        db.add(BackupRun(status="failed", error="boom", finished_at=datetime.now(timezone.utc)))
        await db.flush()
        s = await backup.status(db)
        assert s["overdue"] and s["runs"][0]["error"] == "boom"
        db.add(BackupRun(status="success", finished_at=datetime.now(timezone.utc) - timedelta(minutes=30)))
        await db.flush()
        assert not (await backup.status(db))["overdue"]
        await db.execute(BackupRun.__table__.update().values(finished_at=datetime.now(timezone.utc) - timedelta(hours=4)))
        assert (await backup.status(db))["overdue"]
        await db.rollback()


async def test_metrics_report_age_and_size():
    from moks import jobs
    from moks.observability import BACKUP_LAST_RUN_OK, DATABASE_SIZE, DATABASE_VOLUME

    await jobs.update_durability_metrics()
    assert DATABASE_SIZE._value.get() > 0 and DATABASE_VOLUME._value.get() == 10 * 1024**3
    async with session_scope() as db:
        db.add(BackupRun(status="failed", finished_at=datetime.now(timezone.utc)))
    await jobs.update_durability_metrics()
    assert BACKUP_LAST_RUN_OK._value.get() == 0
    async with session_scope() as db:
        await db.execute(BackupRun.__table__.delete())


async def test_only_audit_readers_see_backup_status(client):
    from tests.conftest import login, make_user

    tag = uuid.uuid4().hex[:6]
    await make_user(f"v-{tag}@example.com", ["viewer:*:*"])
    await login(client, f"v-{tag}@example.com")
    assert (await client.get("/moks/api/admin/backups")).status_code == 403
    await make_user(f"a-{tag}@example.com", ["admin"])
    await login(client, f"a-{tag}@example.com")
    assert "overdue" in (await client.get("/moks/api/admin/backups")).json()


@pytest.mark.skipif(not shutil.which("pg_dump"), reason="needs PostgreSQL client tools")
async def test_a_clean_chain_verifies_after_a_real_restore(tmp_path):
    """A database of its own, so no other test's tampering is involved."""
    name, scratch = f"moks_src_{uuid.uuid4().hex[:8]}", f"moks_rst_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(f"{_base}/postgres")
    await admin.execute(f'CREATE DATABASE "{name}"')
    await admin.execute(f'CREATE DATABASE "{scratch}"')
    await admin.close()
    try:
        subprocess.run([sys.executable, "-c", "from moks.cli import migrate; migrate()"], check=True, cwd=Path(__file__).parents[1], capture_output=True,
                       env={**os.environ, "MOKS_DATABASE_URL": f"{_base.replace('postgresql://', 'postgresql+asyncpg://')}/{name}"})
        src = await asyncpg.connect(f"{_base}/{name}")
        for i in range(50):
            await src.execute("INSERT INTO audit_events (action, details) VALUES ($1, $2::jsonb)", f"test.{i}", '{"text": "naïve ✓", "n": 1.50, "z": [1, {"b": 2, "a": 1}]}')
        await src.close()
        dump = tmp_path / "clean.dump"
        subprocess.run(["pg_dump", f"{_base}/{name}", "--format=custom", f"--file={dump}"], check=True)
        subprocess.run(["pg_restore", "--no-owner", f"--dbname={_base}/{scratch}", "--exit-on-error", str(dump)], check=True)
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(f"{_base.replace('postgresql://', 'postgresql+asyncpg://')}/{scratch}")
        async with async_sessionmaker(engine)() as db:
            result = await audit.verify_chain(db)
        await engine.dispose()
        assert result["ok"] and result["checked"] >= 50
        check = await asyncpg.connect(f"{_base}/{scratch}")
        with pytest.raises(asyncpg.PostgresError, match="append-only"):
            await check.execute("DELETE FROM audit_events")  # the restored copy is still protected
        await check.close()
    finally:
        admin = await asyncpg.connect(f"{_base}/postgres")
        for db in (name, scratch):
            await admin.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        await admin.close()


@pytest.mark.skipif(not shutil.which("pg_dump"), reason="needs PostgreSQL client tools")
async def test_full_cycle_dump_encrypt_upload_restore_and_verify_audit(tmp_path, client):
    """The whole path except the real bucket: restored row counts match and the audit chain verifies."""
    await audit.record(None, action="backup.test", details={"marker": uuid.uuid4().hex})
    url = f"{_base}/{DB_NAME}"
    dump = tmp_path / "moks.dump"
    version = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True).stdout
    made = subprocess.run(["pg_dump", url, "--format=custom", f"--file={dump}"], capture_output=True, text=True)
    if made.returncode:
        pytest.skip(f"pg_dump cannot dump this server: {version.strip()}")
    s3 = S3()
    async with session_scope() as db:
        source = await backup.table_counts(db)
        source_chain = await audit.verify_chain(db)
    result = backup.publish(dump, backup.Store("b", client=s3), "moks", KEY, {"tables": source}, datetime.now(timezone.utc), backup.Retention())
    assert hashlib.sha256(s3.objects[result["key"]]).hexdigest() == result["sha256"]

    plain = tmp_path / "restored.dump"
    plain.write_bytes(opened(s3.objects[result["key"]]))
    scratch = f"moks_restore_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(f"{_base}/postgres")
    await admin.execute(f'CREATE DATABASE "{scratch}"')
    await admin.close()
    try:
        done = subprocess.run(["pg_restore", "--no-owner", f"--dbname={_base}/{scratch}", "--exit-on-error", str(plain)], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr[:400]
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(f"{_base.replace('postgresql://', 'postgresql+asyncpg://')}/{scratch}")
        async with async_sessionmaker(engine)() as db:
            restored = await backup.table_counts(db)
            chain = await audit.verify_chain(db)
        await engine.dispose()
        cmp = backup.compare_counts(source, restored)
        assert cmp["ok"] and not cmp["differ"], cmp
        # Other tests in this database tamper with a row on purpose, so the proof is that the restored
        # chain gives the same verdict as its source. test_a_clean_chain... covers the clean case.
        assert chain == source_chain, (chain, source_chain)
    finally:
        admin = await asyncpg.connect(f"{_base}/postgres")
        await admin.execute(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
        await admin.close()
