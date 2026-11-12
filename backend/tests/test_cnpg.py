"""CloudNativePG on the Backups page: read-only status and deterministic findings."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from moks import cnpg
from moks.connectors import registry
from moks.connectors.base import ConnectorError
from tests.conftest import login, make_env, make_user
from tests.fakes import FakeKubernetes

NOW = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)


def iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def cluster(name="postgres-cluster", ns="postgres-db", archiving="True", backup=True, **status):
    spec = {"instances": 3, "imageName": "ghcr.io/cloudnative-pg/postgresql:17.6", "storage": {"size": "20Gi"}}
    if backup:
        spec["backup"] = {"barmanObjectStore": {"destinationPath": "s3://backups/postgres", "s3Credentials": {"accessKeyId": {"name": "r2", "key": "id"}}}, "retentionPolicy": "30d"}
    conditions = [{"type": "ContinuousArchiving", "status": archiving, "message": "Continuous archiving is working" if archiving == "True" else "exit status 1"}] if backup else []
    return {"metadata": {"name": name, "namespace": ns, "uid": "c-1"}, "spec": spec,
            "status": {"phase": "Cluster in healthy state", "readyInstances": 3, "currentPrimary": f"{name}-1", "conditions": conditions,
                       "firstRecoverabilityPoint": iso(240), **status}}


def backup(name, hours_ago, phase="completed", cluster_name="postgres-cluster", ns="postgres-db", schedule="daily", error=None):
    status = {"phase": phase, "startedAt": iso(hours_ago), "backupId": "20261112T000000", "destinationPath": "s3://backups/postgres"}
    if phase == "completed":
        status["stoppedAt"] = iso(hours_ago - 0.025)
    if error:
        status["error"] = error
    return {"metadata": {"name": name, "namespace": ns, "labels": {"cnpg.io/scheduled-backup": schedule} if schedule else {}},
            "spec": {"cluster": {"name": cluster_name}, "method": "barmanObjectStore"}, "status": status}


def scheduled(name="daily", suspend=False, cluster_name="postgres-cluster", ns="postgres-db"):
    return {"metadata": {"name": name, "namespace": ns}, "spec": {"schedule": "0 0 2 * * *", "cluster": {"name": cluster_name}, "suspend": suspend},
            "status": {"lastScheduleTime": iso(4), "nextScheduleTime": iso(-20)}}


def state(clusters, backups=(), schedules=()):
    rows = sorted((cnpg.backup_row(b) for b in backups), key=lambda r: r["started"] or "", reverse=True)
    return {"installed": True, "clusters": [cnpg.cluster_row(c) for c in clusters], "backups": rows, "schedules": [cnpg.schedule_row(s) for s in schedules]}


def codes(s) -> set[str]:
    return {f["code"].split(":")[0] for f in cnpg.findings(s, NOW)}


def test_a_healthy_cluster_has_no_findings():
    s = state([cluster()], [backup("b2", 4), backup("b1", 28)], [scheduled()])
    assert cnpg.findings(s, NOW) == []
    row = s["clusters"][0]
    assert row["healthy"] and row["archiving"] is True and row["backup_method"] == "barmanObjectStore" and row["destination"] == "s3://backups/postgres"
    assert s["backups"][0]["name"] == "b2" and s["backups"][0]["duration_seconds"] == 90 and s["backups"][0]["schedule"] == "daily"


def test_rows_never_carry_credential_references():
    assert "s3Credentials" not in str(cnpg.cluster_row(cluster())) and "accessKeyId" not in str(cnpg.cluster_row(cluster()))


def test_a_cluster_without_backup_is_one_finding_not_four():
    assert codes(state([cluster(backup=False)])) == {"cnpg_unconfigured"}


def test_failing_archiving_stale_backup_and_missing_schedule_are_each_reported():
    s = state([cluster(archiving="False")], [backup("old", 40)])
    found = cnpg.findings(s, NOW)
    assert {f["code"].split(":")[0] for f in found} == {"cnpg_archiving", "cnpg_stale", "cnpg_unscheduled"}
    assert next(f for f in found if f["code"].startswith("cnpg_archiving"))["severity"] == "bad" and "exit status 1" in found[0]["text"]


def test_no_backup_a_failed_latest_and_suspended_schedules():
    assert codes(state([cluster()], [], [scheduled()])) == {"cnpg_no_backup"}
    s = state([cluster()], [backup("new", 1, phase="failed", error="connection refused"), backup("ok", 5)], [scheduled(suspend=True)])
    assert codes(s) == {"cnpg_last_failed", "cnpg_suspended"}


def test_the_cluster_status_counts_when_old_backup_objects_were_pruned():
    assert codes(state([cluster(lastSuccessfulBackup=iso(3))], [], [scheduled()])) == set()


def test_backups_of_another_cluster_do_not_cover_this_one():
    s = state([cluster(), cluster(name="other")], [backup("b", 2)], [scheduled()])
    assert {f["code"] for f in cnpg.findings(s, NOW)} == {"cnpg_no_backup:postgres-db/other", "cnpg_unscheduled:postgres-db/other"}


def test_the_plugin_archiver_counts_as_configured():
    c = cluster(backup=False)
    c["spec"]["plugins"] = [{"name": "barman-cloud.cloudnative-pg.io", "isWALArchiver": True, "parameters": {"barmanObjectName": "r2-store"}}]
    row = cnpg.cluster_row(c)
    assert row["backup_method"] == "plugin" and row["destination"] == "r2-store" and row["archiving"] is None


@pytest.fixture
async def world(monkeypatch):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "kubernetes", FakeKubernetes)
    registry._cache.clear()
    yield
    registry._cache.clear()


async def _env():
    name = f"pg-{uuid.uuid4().hex[:6]}"
    await make_env(name, connectors=[{"type": "kubernetes", "name": "k8s", "config": {"mode": "in_cluster"}}])
    tag = uuid.uuid4().hex[:6]
    wide, scoped = f"op-{tag}@example.com", f"sc-{tag}@example.com"
    await make_user(wide, [f"operator:{name}:*"])
    await make_user(scoped, [f"operator:{name}:shop"])
    return name, wide, scoped


async def test_the_api_lists_clusters_and_needs_cluster_access(client, world, monkeypatch):
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    done = backup("b1", 0)
    done["status"].update(startedAt=recent.isoformat(), stoppedAt=(recent + timedelta(seconds=30)).isoformat())
    monkeypatch.setattr(FakeKubernetes, "cnpg", {"clusters": [cluster()], "backups": [done], "scheduledbackups": [scheduled()]})
    name, wide, scoped = await _env()
    await login(client, wide)
    r = await client.get(f"/moks/api/backups/{name}/postgres")
    body = r.json()
    assert r.status_code == 200 and body["installed"] and body["findings"] == []
    assert body["clusters"][0]["name"] == "postgres-cluster" and body["backups"][0]["phase"] == "completed" and body["schedules"][0]["cron"] == "0 0 2 * * *"
    assert "s3Credentials" not in r.text
    await login(client, scoped)
    assert (await client.get(f"/moks/api/backups/{name}/postgres")).status_code == 403


async def test_a_cluster_without_the_operator_is_a_state_not_an_error(client, world, monkeypatch):
    monkeypatch.setattr(FakeKubernetes, "cnpg", None)
    name, wide, _ = await _env()
    await login(client, wide)
    body = (await client.get(f"/moks/api/backups/{name}/postgres")).json()
    assert body["installed"] is False and body["clusters"] == [] and body["findings"] == [] and "not installed" in body["error"]


async def test_an_unreachable_api_server_is_a_502(client, world, monkeypatch):
    async def down(self, plural):
        raise ConnectorError("unavailable", "the API server did not answer")
    monkeypatch.setattr(FakeKubernetes, "cnpg_list", down)
    name, wide, _ = await _env()
    await login(client, wide)
    assert (await client.get(f"/moks/api/backups/{name}/postgres")).status_code == 502
