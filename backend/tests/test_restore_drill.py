"""Real pg_dump, pg_restore and decryption. Only the bucket is simulated."""
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest

from moks import backup
from tests.test_backup import KEY, S3, _base

ROOT = Path(__file__).resolve().parents[2]
SQL = ROOT / "deploy/helm/moks/files/consistent-dump.sql"
pytestmark = pytest.mark.skipif(not shutil.which("pg_dump") or not shutil.which("psql"), reason="needs PostgreSQL client tools")


@pytest.fixture
async def source(tmp_path):
    name = f"moks_src_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(f"{_base}/postgres")
    await admin.execute(f'CREATE DATABASE "{name}"')
    await admin.close()
    subprocess.run([sys.executable, "-c", "from moks.cli import migrate; migrate()"], check=True, cwd=ROOT / "backend", capture_output=True,
                   env={**os.environ, "MOKS_DATABASE_URL": f"{_base.replace('postgresql://', 'postgresql+asyncpg://')}/{name}"})
    c = await asyncpg.connect(f"{_base}/{name}")
    for i in range(5):
        await c.execute("INSERT INTO audit_events (action, details) VALUES ($1, '{}'::jsonb)", f"t.{i}")
    await c.close()
    yield name
    admin = await asyncpg.connect(f"{_base}/postgres")
    await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    await admin.close()


def consistent_dump(name: str, work: Path) -> tuple[Path, dict]:
    dump, manifest = work / "moks.dump", work / "manifest.json"
    url = f"{_base}/{name}"
    env = {**os.environ, "MOKS_DUMP_FILE": str(dump), "PGDATABASE": name}
    for part, var in ((url.split("://")[1].split("@")[0].split(":")[0], "PGUSER"),):
        env[var] = part
    env["PGHOST"], env["PGPORT"] = url.split("@")[1].split("/")[0].split(":")
    subprocess.run(["psql", "-X", "-v", f"manifest={manifest}", "-f", str(SQL)], check=True, capture_output=True, text=True, env=env)
    assert not Path(str(dump) + ".failed").exists()
    return dump, json.loads(manifest.read_text())


def publish(dump, manifest, s3, key=KEY):
    return backup.publish(dump, backup.Store("b", client=s3), "moks", key, {**manifest, "counts_source": "snapshot"}, datetime.now(timezone.utc), backup.Retention())


async def drill(s3, object_key, tmp_path, key=KEY):
    work = tmp_path / "drill"
    work.mkdir(exist_ok=True)
    return await backup.restore_drill(backup.Store("b", client=s3), object_key, key, f"{_base}/postgres", "moks_live", work)


async def _no_leftovers():
    admin = await asyncpg.connect(f"{_base}/postgres")
    rows = await admin.fetch("SELECT datname FROM pg_database WHERE datname LIKE 'moks_restore_drill_%'")
    await admin.close()
    return [r[0] for r in rows]


async def test_manifest_and_dump_come_from_one_snapshot(source, tmp_path):
    """Rows added while the dump runs appear in neither, so the two always agree."""
    dump, manifest = consistent_dump(source, tmp_path)
    c = await asyncpg.connect(f"{_base}/{source}")
    await c.execute("INSERT INTO audit_events (action, details) VALUES ('after', '{}'::jsonb)")
    live = await c.fetchval("SELECT count(*) FROM audit_events")
    await c.close()
    assert manifest["tables"]["audit_events"] == live - 1
    assert manifest["migration"] and manifest["snapshot_at"]
    s3 = S3()
    r = publish(dump, manifest, s3)
    assert (await drill(s3, r["key"], tmp_path))["ok"]


async def test_successful_isolated_restore_measures_everything(source, tmp_path):
    dump, manifest = consistent_dump(source, tmp_path)
    s3 = S3()
    r = publish(dump, manifest, s3)
    report = await drill(s3, r["key"], tmp_path)
    assert report["ok"], report["failures"]
    assert report["smoke"]["audit_chain_ok"] and report["smoke"]["audit_rows"] >= 5
    assert report["recovery_time_seconds"] > 0 and report["recovery_point_seconds"] >= 0
    assert set(report["seconds"]) == {"download", "decrypt", "restore", "verify"}
    assert await _no_leftovers() == []


async def test_a_missing_table_fails(source, tmp_path):
    dump, manifest = consistent_dump(source, tmp_path)
    manifest["tables"]["table_that_was_lost"] = 3
    s3 = S3()
    report = await drill(s3, publish(dump, manifest, s3)["key"], tmp_path)
    assert not report["ok"] and report["counts"]["missing"] == ["table_that_was_lost"]


async def test_missing_rows_fail(source, tmp_path):
    dump, manifest = consistent_dump(source, tmp_path)
    manifest["tables"]["audit_events"] += 2  # the backup claims more than it restored
    s3 = S3()
    report = await drill(s3, publish(dump, manifest, s3)["key"], tmp_path)
    assert not report["ok"] and "audit_events" in report["counts"]["differ"]


async def test_a_wrong_schema_version_fails(source, tmp_path):
    dump, manifest = consistent_dump(source, tmp_path)
    manifest["migration"] = "9999"
    s3 = S3()
    report = await drill(s3, publish(dump, manifest, s3)["key"], tmp_path)
    assert not report["ok"] and any("schema" in f for f in report["failures"])


async def test_corrupted_ciphertext_fails_before_anything_is_restored(source, tmp_path):
    dump, manifest = consistent_dump(source, tmp_path)
    s3 = S3()
    key = publish(dump, manifest, s3)["key"]
    blob = bytearray(s3.objects[key])
    blob[100] ^= 0xFF
    s3.objects[key] = bytes(blob)
    report = await drill(s3, key, tmp_path)
    assert not report["ok"] and "checksum" in report["failures"][0] and await _no_leftovers() == []


async def test_tampering_that_matches_the_checksum_is_caught_by_authenticated_decryption(source, tmp_path):
    dump, manifest = consistent_dump(source, tmp_path)
    s3 = S3()
    key = publish(dump, manifest, s3)["key"]
    blob = bytearray(s3.objects[key])
    blob[100] ^= 0xFF
    s3.objects[key] = bytes(blob)
    meta = json.loads(s3.objects[key + ".json"])
    import hashlib

    meta["sha256"] = hashlib.sha256(bytes(blob)).hexdigest()
    s3.objects[key + ".json"] = json.dumps(meta).encode()
    report = await drill(s3, key, tmp_path)
    assert not report["ok"] and "decryption" in report["failures"][0]


async def test_the_wrong_key_fails(source, tmp_path):
    dump, manifest = consistent_dump(source, tmp_path)
    s3 = S3()
    key = publish(dump, manifest, s3)["key"]
    report = await drill(s3, key, tmp_path, key=bytes(32))
    assert not report["ok"] and "decryption" in report["failures"][0]


async def test_broken_invariants_fail(source, tmp_path):
    c = await asyncpg.connect(f"{_base}/{source}")
    await c.execute("ALTER TABLE chunks DROP CONSTRAINT IF EXISTS chunks_document_id_fkey")
    await c.execute("INSERT INTO chunks (id, document_id, ord, heading, text) VALUES (gen_random_uuid(), gen_random_uuid(), 0, '', 'orphan')")
    await c.close()
    dump, manifest = consistent_dump(source, tmp_path)
    s3 = S3()
    report = await drill(s3, publish(dump, manifest, s3)["key"], tmp_path)
    assert not report["ok"] and "chunks_have_documents" in report["invariants"]


def test_a_drill_cannot_touch_the_live_database():
    with pytest.raises(backup.BackupError):
        backup.assert_isolated("postgresql://u@h/postgres", "moks", "moks")
    with pytest.raises(backup.BackupError):
        backup.assert_isolated("postgresql://u@h/moks", "moks", "moks_restore_drill_ab")
    backup.assert_isolated("postgresql://u@h/postgres", "moks", "moks_restore_drill_ab")


