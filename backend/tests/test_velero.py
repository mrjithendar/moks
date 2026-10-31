"""Cluster backups: every write is a reviewed plan, and Secret values never surface."""
import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

from moks import velero
from moks.actions import executor as ex
from moks.connectors import registry
from moks.connectors.base import ConnectorError
from moks.db import session_scope
from moks.models import ActionPlan, AuditEvent, BackupStore, Environment
from tests.conftest import login, make_env, make_user
from tests.fakes import FakeKubernetes, FakeWriter

SSE = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
SECRETS = {"MOKS_CRED_R2_ACCESS_KEY_ID": "key-id-value", "MOKS_CRED_R2_SECRET_ACCESS_KEY": "secret-value-do-not-leak", "MOKS_CRED_R2_SSE_KEY": SSE,
           "MOKS_CRED_R2_REPO_PASSWORD": "repository-password-at-least-24"}
REFS = {k: {"type": "env", "name": f"MOKS_CRED_R2_{k.upper()}"} for k in velero.CREDENTIAL_KEYS}


class Writer(FakeWriter):
    def __init__(self):
        super().__init__()
        self.objects: dict[tuple[str, str], dict] = {}
        self.secrets: dict[str, dict] = {}
        self.phase = "InProgress"

    async def velero_get(self, plural, name):
        if (plural, name) in self.objects:
            return self.objects[(plural, name)]
        return await FakeKubernetes({}).velero_get(plural, name)

    async def velero_create(self, plural, body):
        body = {**body, "status": {"phase": self.phase if plural != "backupstoragelocations" else "Available"}}
        self.objects[(plural, body["metadata"]["name"])] = body
        return body

    async def velero_apply(self, plural, name, body):
        return await self.velero_create(plural, body)

    async def write_secret(self, name, data, overwrite):
        self.secrets[name] = data
        return "created"


def bsl(phase="Available"):
    return {"metadata": {"name": velero.LOCATION}, "spec": {"objectStorage": {"bucket": "b", "prefix": "p"}}, "status": {"phase": phase}}


def bk(name="nightly", phase="Completed"):
    return {"metadata": {"name": name, "uid": "u-1"}, "spec": {"includedNamespaces": ["*"], "defaultVolumesToFsBackup": True},
            "status": {"phase": phase, "startTimestamp": "2026-10-30T00:00:00Z", "completionTimestamp": datetime.now(timezone.utc).isoformat(), "errors": 0}}


@pytest.fixture(autouse=True)
async def world(monkeypatch):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "kubernetes", FakeKubernetes)
    registry._cache.clear()
    monkeypatch.setattr(ex, "POLL", 0.01)
    monkeypatch.setattr(ex, "VALIDATION_TIMEOUT", 1)
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(FakeKubernetes, "velero", {"backupstoragelocations": [bsl()], "backups": [bk()], "restores": [], "schedules": []})
    async with session_scope() as db:
        await db.execute(text("DELETE FROM backup_stores"))
        db.add(BackupStore(name="r2", account_id="a" * 32, bucket="backups", prefix="clusters", credentials=REFS))
        await db.execute(text("UPDATE action_plans SET status = 'cancelled' WHERE status IN ('queued', 'executing')"))
    yield
    registry._cache.clear()


async def _env(prod=False, denied=(), kind="eks"):
    name = f"vel-{uuid.uuid4().hex[:6]}"
    env_id = await make_env(name, connectors=[{"type": "kubernetes", "name": "k8s", "config": {"mode": "in_cluster", "executor": {"mode": "in_cluster"}}}])
    async with session_scope() as db:
        e = await db.get(Environment, env_id)
        e.is_production, e.denied_namespaces, e.kind = prod, list(denied), kind
    tag = uuid.uuid4().hex[:6]
    op, ap, scoped = f"op-{tag}@example.com", f"ap-{tag}@example.com", f"sc-{tag}@example.com"
    await make_user(op, [f"operator:{name}:*"])
    await make_user(ap, [f"approver:{name}:*"])
    await make_user(scoped, [f"operator:{name}:shop"])
    return name, op, ap, scoped


async def _run_plan(client, t, pid, writer):
    h = await login(client, t[1])
    p = (await client.get(f"/moks/api/actions/plans/{pid}")).json()
    if p["requires_approval"]:
        ha = await login(client, t[2])
        assert (await client.post(f"/moks/api/actions/plans/{pid}/approve", json={"plan_hash": p["plan_hash"]}, headers=ha)).status_code == 200
        h = await login(client, t[1])
    r = await client.post(f"/moks/api/actions/plans/{pid}/execute", json={"plan_hash": p["plan_hash"]}, headers=h)
    assert r.status_code == 200, r.text

    async def factory(env, connector_id=None):
        return writer
    ex.writer_factory = factory
    await ex.execute_plan(await ex.claim_plan("t"))
    async with session_scope() as db:
        return await db.get(ActionPlan, uuid.UUID(pid))


async def test_status_reports_findings_and_needs_cluster_access(client):
    t = await _env()
    h = await login(client, t[1])
    r = await client.get(f"/moks/api/backups/{t[0]}", headers=h)
    assert r.status_code == 200 and {f["code"] for f in r.json()["findings"]} == {"no_schedule"}
    await login(client, t[3])
    assert (await client.get(f"/moks/api/backups/{t[0]}")).status_code == 403


async def test_velero_missing_is_a_clear_state(client, monkeypatch):
    async def gone(self, plural):
        raise ConnectorError("not_found", "no such resource")
    monkeypatch.setattr(FakeKubernetes, "velero_list", gone)
    t = await _env()
    await login(client, t[1])
    body = (await client.get(f"/moks/api/backups/{t[0]}")).json()
    assert body["installed"] is False and body["findings"][0]["code"] == "not_installed"


async def test_a_namespace_scoped_user_cannot_plan_cluster_wide_work(client):
    t = await _env()
    h = await login(client, t[3])
    r = await client.post(f"/moks/api/backups/{t[0]}/restore", json={"backup": "nightly", "reason": "try to restore"}, headers=h)
    assert r.status_code == 403


async def test_the_generic_plan_endpoint_cannot_reach_these_actions(client):
    t = await _env()
    h = await login(client, t[1])
    r = await client.post("/moks/api/actions/plans", json={"environment": t[0], "namespace": "shop", "action": "velero_restore", "target_kind": "Restore", "target_name": "nightly",
                                                           "params": {"included_namespaces": ["*"]}, "reason": "around the scope check"}, headers=h)
    assert r.status_code == 400
    assert not any(a["action"].startswith("velero") for a in (await client.get("/moks/api/actions/catalog")).json())


async def test_backup_runs_and_excludes_denied_namespaces(client):
    t = await _env(denied=["kube-system"])
    h = await login(client, t[1])
    pid = (await client.post(f"/moks/api/backups/{t[0]}/backup", json={"reason": "before the upgrade"}, headers=h)).json()["plan_id"]
    w = Writer()
    done = await _run_plan(client, t, pid, w)
    assert done.status == "succeeded", done.result
    spec = next(v for k, v in w.objects.items() if k[0] == "backups")["spec"]
    assert spec["includedNamespaces"] == ["*"] and set(spec["excludedNamespaces"]) == {"kube-system", "velero"} and spec["defaultVolumesToFsBackup"] is True


async def test_restore_needs_a_second_person_and_shows_what_it_overwrites(client):
    t = await _env()
    h = await login(client, t[1])
    r = await client.post(f"/moks/api/backups/{t[0]}/restore", json={"backup": "nightly", "reason": "bad change", "existing_resource_policy": "update"}, headers=h)
    pid = r.json()["plan_id"]
    assert r.json()["requires_approval"] is True
    plan = (await client.get(f"/moks/api/actions/plans/{pid}")).json()
    assert "OVERWRITTEN" in plan["expected_impact"] and "Secret objects" in plan["expected_impact"]
    assert (await client.post(f"/moks/api/actions/plans/{pid}/approve", json={"plan_hash": plan["plan_hash"]}, headers=h)).status_code == 403


async def test_restore_takes_a_backup_first_then_restores(client):
    t = await _env()
    h = await login(client, t[1])
    pid = (await client.post(f"/moks/api/backups/{t[0]}/restore", json={"backup": "nightly", "reason": "bad change"}, headers=h)).json()["plan_id"]
    w = Writer()
    w.phase = "Completed"
    done = await _run_plan(client, t, pid, w)
    assert done.status == "succeeded", done.result
    assert [k[0] for k in w.objects] == ["backups", "restores"]
    body = next(v for k, v in w.objects.items() if k[0] == "restores")
    assert body["spec"]["backupName"] == "nightly" and body["spec"]["existingResourcePolicy"] == "none"
    assert {"velero", "moks"} <= set(body["spec"]["excludedNamespaces"])


async def test_a_failed_pre_restore_backup_stops_the_restore(client):
    t = await _env()
    h = await login(client, t[1])
    pid = (await client.post(f"/moks/api/backups/{t[0]}/restore", json={"backup": "nightly", "reason": "bad change"}, headers=h)).json()["plan_id"]
    w = Writer()
    w.phase = "Failed"
    done = await _run_plan(client, t, pid, w)
    assert done.status == "failed" and not any(k[0] == "restores" for k in w.objects)


async def test_restore_refuses_a_replaced_backup(client, monkeypatch):
    t = await _env()
    h = await login(client, t[1])
    pid = (await client.post(f"/moks/api/backups/{t[0]}/restore", json={"backup": "nightly", "reason": "bad change"}, headers=h)).json()["plan_id"]
    monkeypatch.setattr(FakeKubernetes, "velero", {"backupstoragelocations": [bsl()], "backups": [{**bk(), "metadata": {"name": "nightly", "uid": "someone-else"}}]})
    w = Writer()
    done = await _run_plan(client, t, pid, w)
    assert "not the one reviewed" in done.result["error"] and w.objects == {}


async def test_configure_writes_secrets_without_exposing_them(client):
    t = await _env()
    h = await login(client, t[1])
    pid = (await client.post(f"/moks/api/backups/{t[0]}/configure", json={"reason": "first connection"}, headers=h)).json()["plan_id"]
    w = Writer()
    done = await _run_plan(client, t, pid, w)
    assert done.status == "succeeded", done.result
    assert set(w.secrets) == {velero.CREDENTIALS_SECRET, velero.SSE_SECRET, velero.REPO_SECRET}
    loc = w.objects[("backupstoragelocations", velero.LOCATION)]["spec"]
    assert loc["config"]["region"] == "auto" and loc["config"]["s3Url"] == f"https://{'a' * 32}.r2.cloudflarestorage.com" and loc["objectStorage"]["prefix"] == f"clusters/{t[0]}"
    async with session_scope() as db:
        plan = await db.get(ActionPlan, uuid.UUID(pid))
        audited = (await db.execute(select(AuditEvent).where(AuditEvent.resource_id == pid))).scalars().all()
    blob = json.dumps([plan.params, plan.result, plan.checkpoint, plan.expected_impact, [a.details for a in audited]], default=str)
    for value in SECRETS.values():
        assert value not in blob


async def test_changed_settings_after_review_stop_the_plan(client):
    t = await _env()
    h = await login(client, t[1])
    pid = (await client.post(f"/moks/api/backups/{t[0]}/configure", json={"reason": "first connection"}, headers=h)).json()["plan_id"]
    async with session_scope() as db:
        await db.execute(text("UPDATE backup_stores SET bucket = 'attacker-bucket'"))
    w = Writer()
    done = await _run_plan(client, t, pid, w)
    assert "changed after this plan" in done.result["error"] and w.secrets == {}


async def test_store_api_takes_references_never_values(client):
    await make_user("r2admin@example.com", ["admin"])
    h = await login(client, "r2admin@example.com")
    body = {"account_id": "b" * 32, "bucket": "my-bucket", "credentials": REFS}
    assert (await client.put("/moks/api/backups/store", json=body, headers=h)).status_code == 200
    pasted = {**body, "credentials": {**REFS, "secret_access_key": {"type": "env", "name": "MOKS_CRED_X", "value": "pasted"}}}
    assert (await client.put("/moks/api/backups/store", json=pasted, headers=h)).status_code == 422
    assert (await client.put("/moks/api/backups/store", json={**body, "credentials": {**REFS, "sse_key": {"type": "env", "name": "PATH"}}}, headers=h)).status_code == 422
    got = (await client.get("/moks/api/backups/store")).json()["store"]
    assert got["endpoint"] == f"https://{'b' * 32}.r2.cloudflarestorage.com" and SECRETS["MOKS_CRED_R2_SECRET_ACCESS_KEY"] not in json.dumps(got)
    await make_user("r2op@example.com", ["operator"])
    await login(client, "r2op@example.com")
    assert (await client.put("/moks/api/backups/store", json=body)).status_code in (403,)


def test_weak_or_malformed_keys_are_refused_before_anything_is_written(monkeypatch):
    store = BackupStore(name="r2", account_id="a" * 32, bucket="b", prefix="p", credentials=REFS)
    monkeypatch.setenv("MOKS_CRED_R2_SSE_KEY", "not-base64!")
    with pytest.raises(ConnectorError):
        velero.secret_payloads(store)
    monkeypatch.setenv("MOKS_CRED_R2_SSE_KEY", SSE)
    monkeypatch.setenv("MOKS_CRED_R2_REPO_PASSWORD", "short")
    with pytest.raises(ConnectorError):
        velero.secret_payloads(store)


def test_repository_password_is_never_overwritten():
    store = BackupStore(name="r2", account_id="a" * 32, bucket="b", prefix="p", credentials=REFS)
    assert {n: o for n, _, o in velero.secret_payloads(store)}[velero.REPO_SECRET] is False


def test_the_investigation_tool_reports_no_secret_data():
    from moks.tools.builtin import REGISTRY

    spec = REGISTRY["k8s_backup_status"]
    assert spec.read_only and not spec.privileged and spec.cluster_scoped


def test_findings_catch_the_quiet_failures():
    old = bk()
    old["status"]["completionTimestamp"] = "2026-01-01T00:00:00Z"
    state = {"installed": True, "locations": [velero.location_row(bsl("Unavailable"))], "backups": [velero.backup_row(old)], "restores": [], "schedules": [{"name": "d", "paused": True}]}
    assert {f["code"] for f in velero.findings(state)} == {"location_unavailable", "stale", "paused"}


async def test_minikube_is_honest_about_volume_data(client):
    t = await _env(kind="minikube")
    h = await login(client, t[1])
    body = (await client.get(f"/moks/api/backups/{t[0]}")).json()
    assert body["volumes_supported"] is False and "volumes_not_covered" in {f["code"] for f in body["findings"]}
    pid = (await client.post(f"/moks/api/backups/{t[0]}/backup", json={"reason": "before the upgrade", "include_volumes": True}, headers=h)).json()["plan_id"]
    plan = (await client.get(f"/moks/api/actions/plans/{pid}")).json()
    assert plan["params"]["include_volumes"] is False and "WITHOUT PersistentVolume data" in plan["expected_impact"]
    w = Writer()
    await _run_plan(client, t, pid, w)
    assert next(v for k, v in w.objects.items() if k[0] == "backups")["spec"]["defaultVolumesToFsBackup"] is False


async def test_a_volume_plan_cannot_run_on_minikube(client):
    from moks.actions.catalog import BackupP, check_policy

    env = Environment(name="m", kind="minikube", allowed_namespaces=["*"], denied_namespaces=[])
    with pytest.raises(ConnectorError):
        check_policy("velero_backup", BackupP(included_namespaces=["*"], excluded_namespaces=[], include_volumes=True), env, None, "Backup")
