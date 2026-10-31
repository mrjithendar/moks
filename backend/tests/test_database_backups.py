"""Database backups: no approval, the dump reaches R2 encrypted and proven, a bad dump never passes as
a backup, schedules, and the details shown for cluster backups."""
import base64
import gzip
import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text

from moks import backup, dbbackup, velero
from moks.actions import executor as ex
from moks.config import get_settings
from moks.connectors import registry
from moks.db import session_scope
from moks.models import ActionPlan, AuditEvent, BackupStore, DatabaseBackup, DatabaseBackupSchedule, Environment
from tests.conftest import login, make_env, make_user
from tests.fakes import FakeKubernetes, FakeWriter

KEY = base64.b64encode(b"k" * 32).decode()
SECRETS = {"MOKS_CRED_R2_ACCESS_KEY_ID": "id", "MOKS_CRED_R2_SECRET_ACCESS_KEY": "secret", "MOKS_CRED_R2_SSE_KEY": KEY, "MOKS_CRED_R2_REPO_PASSWORD": "repository-password-at-least-24"}
REFS = {k: {"type": "env", "name": f"MOKS_CRED_R2_{k.upper()}"} for k in velero.CREDENTIAL_KEYS}
PG_LISTING = json.dumps({"version": "17.11", "database": "shop", "bytes": 9000, "other_databases": ["legacy"], "tables": [
    {"schema": "public", "name": "orders", "rows": 12, "bytes": 8000}, {"schema": "billing", "name": "invoices", "rows": 3, "bytes": 1000}]})
WORKLOADS = {"shop": [{"kind": "StatefulSet", "name": "orders-db", "images": ["postgres:17.11-alpine"], "ready": 1},
                      {"kind": "Deployment", "name": "catalog-db", "images": ["mongo:8.2"], "ready": 1},
                      {"kind": "Deployment", "name": "cache", "images": ["redis:8.10-alpine"], "ready": 1},
                      {"kind": "Deployment", "name": "web", "images": ["nginx:1"], "ready": 2}],
             "other": [{"kind": "Deployment", "name": "other-db", "images": ["registry.io/x/pgvector:0.8"], "ready": 1}]}


class S3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.corrupt = False

    def upload_file(self, path, bucket, key):
        self.objects[key] = Path(path).read_bytes()

    def get_object(self, Bucket, Key, **sse):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": io.BytesIO(b"junk" if self.corrupt else self.objects[Key])}


class Writer(FakeWriter):
    dump = [b"PGDMP", b"-body-" * 500_000]
    exit_code = 0
    image = "postgres:17.11-alpine"

    async def database_pod(self, ns, kind, name):
        return {"name": f"{name}-0", "container": "db", "image": self.image}

    async def exec_stream(self, ns, pod, container, argv, sink, timeout):
        self.calls.append(("exec_stream", ns, pod, container, argv))
        if argv[-1] == dbbackup.PG_INVENTORY:
            await sink(PG_LISTING.encode())
            return 0, ""
        for part in self.dump:
            await sink(part)
        return self.exit_code, "pg_dump: error: connection lost password=hunter2" if self.exit_code else ""


@pytest.fixture(autouse=True)
async def setup(monkeypatch, tmp_path):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "kubernetes", FakeKubernetes)
    registry._cache.clear()
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    s3 = S3()
    monkeypatch.setattr(velero, "_s3_client", lambda store: s3)
    monkeypatch.setattr(get_settings(), "database_backup_namespaces", "shop")
    monkeypatch.setattr(get_settings(), "database_backup_dir", str(tmp_path))

    async def namespaces(self):
        return ["shop", "other", "kube-system"]

    async def workloads(self, ns):
        return WORKLOADS.get(ns, [])

    async def object_meta(self, namespace, kind, name):
        return {"kind": kind, "name": name, "resourceVersion": "1", "uid": f"uid-{name}", "spec": {}, "owner": None, "generation": 1}
    monkeypatch.setattr(FakeKubernetes, "object_meta", object_meta)
    monkeypatch.setattr(FakeKubernetes, "list_namespaces", namespaces)
    monkeypatch.setattr(FakeKubernetes, "workloads", workloads)
    monkeypatch.setattr(dbbackup, "_task", None)
    async with session_scope() as db:
        await db.execute(text("DELETE FROM backup_stores"))
        await db.execute(text("UPDATE database_backups SET status = 'failed' WHERE status IN ('queued', 'running')"))
        await db.execute(text("UPDATE database_backup_schedules SET paused = true"))
        await db.execute(text("UPDATE action_plans SET status = 'cancelled' WHERE status IN ('queued', 'executing')"))
        db.add(BackupStore(name="r2", account_id="a" * 32, bucket="backups", prefix="clusters", credentials=REFS))
    yield s3
    registry._cache.clear()
    async with session_scope() as db:
        await db.execute(text("UPDATE action_plans SET status = 'cancelled' WHERE status IN ('queued', 'executing')"))
        await db.execute(text("UPDATE database_backups SET status = 'failed' WHERE status IN ('queued', 'running')"))
        await db.execute(text("UPDATE database_backup_schedules SET paused = true"))


async def _env(prod=True):
    name = f"db-{uuid.uuid4().hex[:6]}"
    env_id = await make_env(name, connectors=[{"type": "kubernetes", "name": "k8s", "config": {"mode": "in_cluster", "executor": {"mode": "in_cluster"}}}])
    async with session_scope() as db:
        (await db.get(Environment, env_id)).is_production = prod
    op = f"op-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(op, [f"operator:{name}:*"])
    return name, op, env_id


TARGET = {"namespace": "shop", "workload": "orders-db", "kind": "StatefulSet", "reason": "before the migration"}


async def _start(client, name, op, path="backup", **extra):
    r = await client.post(f"/moks/api/backups/{name}/databases/{path}", json={**TARGET, **extra}, headers=await login(client, op))
    assert r.status_code == 201, r.text
    return r.json()


async def _execute(writer):
    async def factory(env, connector_id=None):
        return writer
    ex.writer_factory = factory
    await ex.execute_plan(await ex.claim_plan("t"))


async def _rows(env_id):
    async with session_scope() as db:
        return (await db.execute(select(DatabaseBackup).where(DatabaseBackup.environment_id == env_id))).scalars().all()


async def _backup(client, env, writer):
    name, op, env_id = env
    out = await _start(client, name, op)
    await _execute(writer)
    await dbbackup.tick(writer_override=writer, wait=True)
    return out, (await _rows(env_id))[-1]


@pytest.mark.parametrize("image,engine", [("postgres:17.11-alpine", "postgresql"), ("pgvector/pgvector:0.8.7-pg17", "postgresql"), ("docker.io/library/mongo:8.2", "mongodb"),
                                          ("reg:5000/x/postgres@sha256:abc", "postgresql"), ("redis:8", None), ("nginx-postgres-exporter:1", None)])
def test_the_engine_comes_from_the_image(image, engine):
    assert dbbackup.engine_of(image) == engine


def test_dumps_stay_out_of_the_folder_velero_owns():
    store = BackupStore(prefix="clusters")
    key = dbbackup.object_key(store, "mk4", "shop", "orders-db", "postgresql", datetime(2026, 10, 31, tzinfo=timezone.utc))
    assert key == "clusters/database-backups/mk4/shop/orders-db/2026-10-31T000000Z.dump.enc"
    assert not key.startswith(velero.env_prefix(store, "mk4") + "/")


def test_contents_have_one_shape_for_each_engine():
    pg = dbbackup.inventory_of("postgresql", "notice\n" + PG_LISTING.replace("}, {", "}, \n {"))  # psql breaks json_agg across lines
    assert (pg["table_count"], pg["row_estimate"], pg["not_included"]) == (2, 15, ["legacy"])
    mongo = dbbackup.inventory_of("mongodb", json.dumps({"version": "8.2", "databases": [{"name": "catalog", "bytes": 5, "tables": [{"name": "products", "rows": 7}]}]}))
    assert (mongo["table_count"], mongo["row_estimate"]) == (1, 7)


@pytest.mark.parametrize("length", [0, 1, backup.CHUNK - 1, backup.CHUNK, backup.CHUNK + 1, backup.CHUNK * 2, backup.CHUNK * 2 + 5])
def test_data_sealed_as_it_arrives_reads_back_with_the_standard_decrypter(length):
    key, data, sealed, plain = b"k" * 32, b"x" * length, io.BytesIO(), io.BytesIO()
    sealer = backup.Sealer(sealed, key)
    for i in range(0, length, 70_000):
        sealer.feed(data[i:i + 70_000])
    sealer.close()
    sealed.seek(0)
    backup.decrypt_stream(sealed, plain, key)
    assert plain.getvalue() == data


async def test_a_backup_in_production_needs_no_approval_and_reaches_r2_encrypted(client, setup):
    env = await _env(prod=True)
    w = Writer()
    out, row = await _backup(client, env, w)
    assert out["requires_approval"] is False and row.status == "success" and row.error is None
    stored = setup.objects[row.object_key]
    assert b"-body-" not in stored and row.size_bytes == len(stored) and row.dump_bytes == sum(map(len, w.dump))
    plain = io.BytesIO()
    backup.decrypt_stream(io.BytesIO(stored), plain, b"k" * 32)
    assert plain.getvalue() == b"".join(w.dump)
    manifest = json.loads(setup.objects[row.object_key + ".json"])
    assert manifest["sha256"] == row.sha256 and manifest["inventory"]["databases"][0]["tables"][0]["name"] == "orders" and manifest["restore"]
    assert row.inventory["table_count"] == 2 and row.image == w.image and row.pod == "orders-db-0"
    async with session_scope() as db:
        plan = await db.get(ActionPlan, uuid.UUID(out["plan_id"]))
        assert plan.status == "succeeded" and row.plan_id == plan.id
        audit = (await db.execute(select(AuditEvent).where(AuditEvent.action == "database_backup.run").order_by(AuditEvent.id.desc()).limit(1))).scalar_one()
    assert audit.details["sha256"] == row.sha256 and audit.outcome == "success"
    assert not list(Path(get_settings().database_backup_dir).iterdir())  # nothing left behind


async def test_only_the_fixed_commands_run_and_none_holds_a_login(client):
    env = await _env()
    w = Writer()
    await _backup(client, env, w)
    runs = [c[4] for c in w.calls if c[0] == "exec_stream"]
    assert runs == [dbbackup.ENGINES["postgresql"].inventory, dbbackup.ENGINES["postgresql"].dump]
    assert '"$POSTGRES_USER"' in runs[1][2] and not any("password" in part.lower() for part in runs[1])
    assert '"$MONGO_INITDB_ROOT_PASSWORD"' in dbbackup.ENGINES["mongodb"].dump[2]


@pytest.mark.parametrize("attrs,reason", [({"exit_code": 1}, "failed (exit 1)"), ({"exit_code": None}, "failed (exit None)"), ({"dump": [b"<html>not a dump"]}, "did not produce"),
                                          ({"dump": []}, "did not produce"), ({"image": "mongo:8.2"}, "now runs")])
async def test_a_bad_dump_never_counts_as_a_backup(client, setup, attrs, reason):
    env = await _env()
    w = Writer()
    for k, v in attrs.items():
        setattr(w, k, v)
    _, row = await _backup(client, env, w)
    assert row.status == "failed" and reason in row.error and row.object_key is None and not setup.objects
    assert "hunter2" not in row.error
    assert not list(Path(get_settings().database_backup_dir).iterdir())


async def test_a_copy_that_does_not_read_back_fails(client, setup):
    env = await _env()
    setup.corrupt = True
    _, row = await _backup(client, env, Writer())
    assert row.status == "failed" and "does not match" in row.error


async def test_a_schedule_runs_at_its_interval_and_a_pause_stops_it(client, setup):
    name, op, env_id = await _env()
    await _start(client, name, op, "schedule", interval_hours=6)
    w = Writer()
    await _execute(w)
    await dbbackup.tick(writer_override=w, wait=True)
    rows = await _rows(env_id)
    assert [(r.status, r.trigger) for r in rows] == [("success", "schedule")]
    async with session_scope() as db:
        sched = (await db.execute(select(DatabaseBackupSchedule).where(DatabaseBackupSchedule.environment_id == env_id))).scalar_one()
        assert sched.next_run_at > datetime.now(timezone.utc) + timedelta(hours=5)
    await dbbackup.tick(writer_override=w, wait=True)
    assert len(await _rows(env_id)) == 1  # not due yet
    async with session_scope() as db:
        sched = await db.get(DatabaseBackupSchedule, sched.id)
        sched.next_run_at, sched.paused = datetime.now(timezone.utc) - timedelta(minutes=1), True
    await dbbackup.tick(writer_override=w, wait=True)
    assert len(await _rows(env_id)) == 1


async def test_a_run_left_behind_by_a_restart_is_marked_failed(client, setup):
    name, op, env_id = await _env()
    async with session_scope() as db:
        db.add(DatabaseBackup(environment_id=env_id, namespace="shop", workload="orders-db", workload_kind="StatefulSet", engine="postgresql", status="running",
                              started_at=datetime.now(timezone.utc) - timedelta(days=1)))
    await dbbackup.tick(writer_override=Writer(), wait=True)
    row = (await _rows(env_id))[0]
    assert row.status == "failed" and "executor stopped" in row.error


async def test_the_page_shows_targets_gaps_and_contents(client):
    env = await _env()
    await _backup(client, env, Writer())
    d = (await client.get(f"/moks/api/backups/{env[0]}/databases", headers=await login(client, env[1]))).json()
    assert {(t["workload"], t["engine"], t["enabled"]) for t in d["targets"]} == {("orders-db", "postgresql", True), ("catalog-db", "mongodb", True), ("other-db", "postgresql", False)}
    assert d["not_covered"] == [{"namespace": "shop", "workload": "cache", "kind": "Deployment", "ready": 1, "service": "redis"}]
    assert d["backups"][0]["inventory"]["databases"][0]["tables"][1]["schema"] == "billing"
    codes = {f["code"] for f in d["findings"]}
    assert {"not_enabled:other/other-db", "never:shop/catalog-db", "no_schedule:shop/orders-db", "partial:shop/orders-db"} <= codes and "never:shop/orders-db" not in codes


async def test_only_real_databases_in_enabled_namespaces_can_be_targeted(client):
    name, op, _ = await _env()
    h = await login(client, op)
    url = f"/moks/api/backups/{name}/databases/backup"
    assert (await client.post(url, json={**TARGET, "workload": "web", "kind": "Deployment"}, headers=h)).status_code == 404
    assert (await client.post(url, json={**TARGET, "namespace": "other", "workload": "other-db", "kind": "Deployment"}, headers=h)).status_code == 409
    around = await client.post("/moks/api/actions/plans", json={"environment": name, "namespace": "shop", "action": "database_backup", "target_kind": "Deployment",
                                                                "target_name": "web", "params": {"engine": "postgresql"}, "reason": "around the check"}, headers=h)
    assert around.status_code == 400
    assert not any(a["action"].startswith("database") for a in (await client.get("/moks/api/actions/catalog", headers=h)).json())


async def test_someone_without_namespace_rights_cannot_start_one(client):
    name, op, _ = await _env()
    viewer = f"v-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(viewer, [f"viewer:{name}:*"])
    r = await client.post(f"/moks/api/backups/{name}/databases/backup", json=TARGET, headers=await login(client, viewer))
    assert r.status_code == 403


class Frames:
    """What the Kubernetes exec websocket delivers: a channel byte, then data."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.closed = False

    async def receive(self):
        from types import SimpleNamespace
        return SimpleNamespace(type=2, data=self.frames.pop(0)) if self.frames else SimpleNamespace(type=8, data=None)

    async def close(self):
        self.closed = True


class Ws:
    def __init__(self, configuration=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def _stream(monkeypatch, frames):
    from types import SimpleNamespace

    import kubernetes_asyncio.client as kc
    import kubernetes_asyncio.stream as ks

    from moks.actions.writer import KubernetesWriter

    resp = Frames(frames)

    class Core:
        def __init__(self, api_client=None):
            pass

        async def connect_get_namespaced_pod_exec(self, pod, ns, **kw):
            assert kw["container"] == "db" and kw["stdin"] is False and kw["tty"] is False

            async def connected():  # as the real client: this returns a connection still to be awaited
                return resp
            return connected()

    async def client():
        return SimpleNamespace(configuration=None)
    monkeypatch.setattr(ks, "WsApiClient", Ws)
    monkeypatch.setattr(kc, "CoreV1Api", Core)
    writer = KubernetesWriter.__new__(KubernetesWriter)
    writer.read = SimpleNamespace(_client=client)
    out = bytearray()

    async def sink(part):
        out.extend(part)
    code, err = await writer.exec_stream("shop", "p", "db", ["true"], sink, 5)
    assert resp.closed
    return code, err, bytes(out)


async def test_the_stream_separates_data_errors_and_exit_status(monkeypatch):
    done = b"\x03" + json.dumps({"status": "Success"}).encode()
    assert await _stream(monkeypatch, [b"\x01PGDMP", b"\x02warning", b"\x01rest", done]) == (0, "warning", b"PGDMPrest")
    failed = b"\x03" + json.dumps({"status": "Failure", "details": {"causes": [{"reason": "ExitCode", "message": "3"}]}}).encode()
    assert (await _stream(monkeypatch, [b"\x01x", failed]))[0] == 3


async def test_a_stream_cut_before_its_exit_status_has_no_exit_code(monkeypatch):
    assert (await _stream(monkeypatch, [b"\x01half a dump"]))[0] is None


# ---- what is inside a cluster backup ---------------------------------------------------------

def test_contents_are_grouped_by_kind_and_namespace():
    out = velero.summarize_contents({"apps/v1/Deployment": ["shop/web", "shop/api"], "v1/Secret": ["moks/token"], "v1/Namespace": ["shop"]})
    assert out["total"] == 4 and out["kinds"][0] == {"kind": "Deployment", "count": 2}
    assert {n["namespace"]: n["count"] for n in out["namespaces"]} == {"(cluster)": 1, "moks": 1, "shop": 2}
    assert {"kind": "Secret", "namespace": "moks", "name": "token"} in out["items"]


def _velero_backup(phase="Completed"):
    return {"metadata": {"name": "nightly", "uid": "u1", "creationTimestamp": "2026-10-31T10:49:10Z", "labels": {"velero.io/storage-location": "moks-r2"}},
            "spec": {"includedNamespaces": ["*"], "excludedNamespaces": ["velero"], "excludedResources": ["volumesnapshots"], "includeClusterResources": True,
                     "storageLocation": "moks-r2", "ttl": "87600h"},
            "status": {"phase": phase, "startTimestamp": "2026-10-31T10:49:10Z", "completionTimestamp": "2026-10-31T10:49:25Z", "formatVersion": "1.1.0",
                       "progress": {"itemsBackedUp": 3, "totalItems": 3}}}


async def test_details_show_metadata_and_contents(client, setup, monkeypatch):
    name, op, _ = await _env()
    monkeypatch.setattr(FakeKubernetes, "velero", {"backups": [_velero_backup()]})
    base = f"clusters/{name}/backups/nightly/nightly"
    setup.objects[f"{base}-resource-list.json.gz"] = gzip.compress(json.dumps({"v1/Pod": ["shop/a", "shop/b"], "v1/Namespace": ["shop"]}).encode())
    setup.objects[f"{base}-results.gz"] = gzip.compress(json.dumps({"warnings": {"cluster": ["slow"]}}).encode())
    d = (await client.get(f"/moks/api/backups/{name}/items/nightly", headers=await login(client, op))).json()
    assert d["duration_seconds"] == 15 and d["ttl"] == "87600h" and d["format_version"] == "1.1.0" and d["excluded_namespaces"] == ["velero"]
    assert d["contents"]["total"] == 3 and d["contents"]["warnings"] == {"cluster": ["slow"]}


async def test_details_still_show_metadata_when_the_list_cannot_be_read(client, monkeypatch):
    name, op, _ = await _env()
    monkeypatch.setattr(FakeKubernetes, "velero", {"backups": [_velero_backup()]})
    d = (await client.get(f"/moks/api/backups/{name}/items/nightly", headers=await login(client, op))).json()
    assert d["contents"] is None and "could not be read" in d["contents_note"] and d["items"] == 3


async def test_details_need_the_same_access_as_the_backups_page(client):
    name, op, _ = await _env()
    scoped = f"s-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(scoped, [f"operator:{name}:shop"])
    assert (await client.get(f"/moks/api/backups/{name}/items/nightly", headers=await login(client, scoped))).status_code == 403


# ---- no approval for backups ------------------------------------------------------------------

async def test_cluster_backups_start_without_approval_in_production_and_restores_do_not(client, monkeypatch):
    name, op, _ = await _env(prod=True)
    bsl = {"metadata": {"name": velero.LOCATION}, "status": {"phase": "Available"}, "spec": {"objectStorage": {"bucket": "b"}}}
    monkeypatch.setattr(FakeKubernetes, "velero", {"backupstoragelocations": [bsl], "backups": [_velero_backup()]})
    h = await login(client, op)
    for path, body in (("backup", {"reason": "before the upgrade"}), ("schedule", {"reason": "nightly run", "name": "daily", "cron": "0 2 * * *"}), ("configure", {"reason": "new bucket"})):
        r = await client.post(f"/moks/api/backups/{name}/{path}", json=body, headers=h)
        assert r.status_code == 201 and r.json()["requires_approval"] is False, r.text
        assert (await client.get(f"/moks/api/actions/plans/{r.json()['plan_id']}", headers=h)).json()["status"] == "queued"
    r = await client.post(f"/moks/api/backups/{name}/restore", json={"backup": "nightly", "reason": "bad change"}, headers=h)
    assert r.json()["requires_approval"] is True
    assert (await client.get(f"/moks/api/actions/plans/{r.json()['plan_id']}", headers=h)).json()["status"] == "pending"
