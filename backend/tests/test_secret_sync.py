"""Kubernetes <-> AWS Secrets Manager sync: encoding, three-way comparison, reviewed plans, concurrency, failure
recovery, permissions, and keeping values out of everything MOKS stores or emits.

Integration tests run the real API, job queue, executor code paths and AwsSecretsConnector against
tests/secret_fakes.py (in-memory Kubernetes Secrets, a boto-shaped Secrets Manager). No real cluster or AWS.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import uuid

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import select, text

from moks import jobs, observability
from moks.config import get_settings
from moks.connectors import registry
from moks.db import session_scope
from moks.models import Base, SecretMapping
from moks.secrets import compare, tasks
from moks.secrets.aws import AwsSecretsConnector
from moks.secrets.content import (AwsValue, Content, FormatError, content_digest, diff, from_aws, key_digest, merge, summarize, to_aws)
from moks.secrets.kube import consumers_of
from moks.secrets.ownership import aws_manager, kubernetes_manager
from tests.conftest import login, make_env, make_user
from tests.fakes import FakeKubernetes
from tests.secret_fakes import FakeSecretsKube, FakeSecretsManager, FakeSTS, client_error

SENTINEL = "zq-SENTINEL-7f3a9c-value"
KEY = b"k" * 40
ACCOUNT, REGION = "123456789012", "eu-west-1"


# ---- encoding, digests, comparison: pure -------------------------------------------------------------------


MIXED = Content("Opaque", {"password": SENTINEL.encode(), "empty": b"", "bin": b"\x00\xff\xfe", "uni": "héllo ✓ 日本".encode()})


def test_envelope_round_trips_binary_unicode_empty_and_type():
    v = to_aws(MIXED, "moks-k8s-v1", {})
    assert v.string and SENTINEL not in v.string  # base64 inside the envelope, not the raw value
    back = from_aws(v, "moks-k8s-v1", {}, "Opaque")
    assert back == MIXED and back.data["empty"] == b"" and "missing" not in back.data
    tls = Content("kubernetes.io/tls", {"tls.crt": b"c", "tls.key": b"k"})
    assert from_aws(to_aws(tls, "moks-k8s-v1", {}), "moks-k8s-v1", {}, "Opaque").type == "kubernetes.io/tls"


def test_absent_and_empty_keys_stay_distinct_in_digests_and_diffs():
    with_empty = summarize(KEY, Content("Opaque", {"a": b"1", "e": b""}))
    without = summarize(KEY, Content("Opaque", {"a": b"1"}))
    assert content_digest("Opaque", with_empty) != content_digest("Opaque", without)
    d = diff(without, with_empty)
    assert [r["key"] for r in d["removed"]] == ["e"] and d["removed"][0]["from"]["empty"] is True


def test_json_text_binary_formats_and_their_refusals():
    plain = Content("Opaque", {"user": b"app", "pass": "pä".encode()})
    assert json.loads(to_aws(plain, "json", {}).string) == {"pass": "pä", "user": "app"}
    assert from_aws(to_aws(plain, "json", {}), "json", {}, "Opaque") == plain
    with pytest.raises(FormatError) as e:
        to_aws(MIXED, "json", {})
    assert e.value.keys == ["bin"] and SENTINEL not in e.value.message
    # selected keys: renamed, and AWS keys outside the mapping are kept on export
    km = {"keys": {"DB_USER": "user", "DB_PASS": "pass"}}
    current = AwsValue(string=json.dumps({"DB_USER": "old", "other": "keep"}))
    out = json.loads(to_aws(plain, "json", km, current).string)
    assert out == {"DB_USER": "app", "DB_PASS": "pä", "other": "keep"}
    assert from_aws(AwsValue(string=json.dumps(out)), "json", km, "Opaque") == plain
    with pytest.raises(FormatError) as e:
        from_aws(AwsValue(string=json.dumps({"n": 5, "s": "x"})), "json", {}, "Opaque")
    assert e.value.keys == ["n"]
    with pytest.raises(FormatError):
        from_aws(AwsValue(string="not json"), "json", {}, "Opaque")
    one = Content("Opaque", {"token": b"abc"})
    assert to_aws(one, "text", {"key": "token"}).string == "abc"
    with pytest.raises(FormatError):
        to_aws(plain, "text", {"key": "user"})
    blob = Content("Opaque", {"ks": b"\x00\x01"})
    assert from_aws(to_aws(blob, "binary", {"key": "ks"}), "binary", {"key": "ks"}, "Opaque") == blob
    with pytest.raises(FormatError):
        from_aws(AwsValue(string="x"), "binary", {"key": "ks"}, "Opaque")


def test_digests_are_keyed_not_raw_hashes():
    s = summarize(KEY, Content("Opaque", {"p": b"hunter2"}))
    assert s["p"]["d"] != hashlib.sha256(b"hunter2").hexdigest()
    assert s["p"]["d"] != summarize(b"x" * 40, Content("Opaque", {"p": b"hunter2"}))["p"]["d"]
    assert s["p"]["d"] == key_digest(KEY, "p", b"hunter2")
    # the same value under another key name has another digest, so equal values do not show across keys
    assert key_digest(KEY, "a", b"v") != key_digest(KEY, "b", b"v")


def _st(c: Content | None, **kw):
    if c is None:
        return {"exists": False, **kw}
    keys = summarize(KEY, c)
    return {"exists": True, "type": c.type, "keys": keys, "digest": content_digest(c.type, keys), **kw}


@pytest.mark.parametrize("k,a,b,want,proposal", [
    ("v1", "v1", None, "in_sync", None),
    ("v1", "v2", None, "changed_both", None),      # first sync with different values: explicit choice
    ("v2", "v1", "v1", "changed_kubernetes", "kubernetes"),
    ("v1", "v2", "v1", "changed_aws", "aws"),
    ("v2", "v3", "v1", "changed_both", None),
    (None, "v1", "v1", "missing_kubernetes", "aws"),
    ("v1", None, "v1", "missing_aws", "kubernetes"),
])
def test_three_way_classification(k, a, b, want, proposal):
    c = {v: Content("Opaque", {"x": v.encode()}) for v in ("v1", "v2", "v3")}
    base = None if b is None else {"kid": "kid", "digest": _st(c[b])["digest"]}
    cls, first, prop, _ = compare.classify(_st(c.get(k)), _st(c.get(a)), base, "kid")
    assert (cls, prop) == (want, proposal)
    assert first == (b is None and want == "changed_both") or want != "changed_both"


def test_classification_ignores_a_baseline_from_another_digest_key_and_reports_failures():
    c1, c2 = Content("Opaque", {"x": b"1"}), Content("Opaque", {"x": b"2"})
    stale_key = {"kid": "old", "digest": _st(c1)["digest"]}
    assert compare.classify(_st(c2), _st(c1), stale_key, "new")[:2] == ("changed_both", True)
    assert compare.classify({"error": "IAM denied"}, _st(c1), None, "k")[0] == "failed"
    assert compare.classify(_st(c1, unsupported="type"), _st(c1), None, "k")[0] == "unsupported"


def test_edited_resolution_merge_and_digest_known_without_values():
    k = summarize(KEY, Content("Opaque", {"a": b"1", "b": b"2"}))
    a = summarize(KEY, Content("Opaque", {"a": b"9", "c": b"3"}))
    merged = merge(k, a, {"a": "aws", "b": "kubernetes", "c": "omit"})
    assert set(merged) == {"a", "b"} and merged["a"] == a["a"]
    with pytest.raises(FormatError):
        merge(k, a, {"a": "aws"})  # every key needs a choice


def test_ownership_detection_and_exclusions():
    assert kubernetes_manager({}, "kubernetes.io/service-account-token")["excluded"]
    assert kubernetes_manager({"labels": {"owner": "helm"}}, "helm.sh/release.v1")["excluded"]
    assert kubernetes_manager({"ownerReferences": [{"kind": "ExternalSecret", "name": "db"}]}, "Opaque")["manager"] == "external-secrets"
    assert kubernetes_manager({"labels": {"secrets-store.csi.k8s.io/managed": "true"}}, "Opaque")["manager"] == "secrets-store-csi"
    assert kubernetes_manager({"annotations": {"cert-manager.io/certificate-name": "web"}}, "kubernetes.io/tls")["manager"] == "cert-manager"
    assert kubernetes_manager({"labels": {"app.kubernetes.io/managed-by": "Helm"}}, "Opaque")["manager"] == "helm"
    assert kubernetes_manager({"annotations": {"argocd.argoproj.io/tracking-id": "x"}}, "Opaque")["manager"] == "argocd"
    assert kubernetes_manager({"labels": {"kustomize.toolkit.fluxcd.io/name": "apps"}}, "Opaque")["manager"] == "flux"
    assert kubernetes_manager({}, "Opaque")["manager"] is None
    assert aws_manager({"owning_service": "rds"})["manager"] == "aws:rds"
    assert aws_manager({"rotation_enabled": True})["manager"] == "aws-rotation"


def test_workload_impact_says_when_a_restart_is_needed():
    specs = [{"kind": "Deployment", "name": "api", "pod_spec": {"containers": [{"env": [{"name": "P", "valueFrom": {"secretKeyRef": {"name": "db", "key": "password"}}}]}]}},
             {"kind": "Deployment", "name": "files", "pod_spec": {"volumes": [{"name": "v", "secret": {"secretName": "db"}}], "containers": [{"volumeMounts": [{"name": "v", "mountPath": "/s"}]}]}},
             {"kind": "StatefulSet", "name": "sub", "pod_spec": {"volumes": [{"name": "v", "projected": {"sources": [{"secret": {"name": "db"}}]}}],
                                                                 "containers": [{"volumeMounts": [{"name": "v", "mountPath": "/s/p", "subPath": "p"}]}]}},
             {"kind": "Deployment", "name": "pull", "pod_spec": {"imagePullSecrets": [{"name": "db"}], "containers": [{}]}},
             {"kind": "Deployment", "name": "other", "pod_spec": {"containers": [{"envFrom": [{"secretRef": {"name": "else"}}]}]}}]
    got = {c["name"]: c for c in consumers_of("db", specs)}
    assert set(got) == {"api", "files", "sub", "pull"}
    assert got["api"]["restart_required"] and got["api"]["keys"] == ["password"]
    assert not got["files"]["restart_required"] and got["sub"]["restart_required"] and not got["pull"]["restart_required"]


def test_aws_connector_boundaries():
    from moks.connectors.base import ConnectorError

    base = {"account_id": ACCOUNT, "region": REGION, "allowed_prefixes": ["moks/"], "kubernetes_namespaces": ["shop"]}
    with pytest.raises(ConnectorError):  # a cross-account role must live in the configured account
        AwsSecretsConnector({**base, "role_arn": "arn:aws:iam::999999999999:role/x"})
    with pytest.raises(ConnectorError):
        AwsSecretsConnector({**base, "allowed_prefixes": []})
    with pytest.raises(ConnectorError):
        AwsSecretsConnector({**base, "recovery_window_days": 3})
    c = AwsSecretsConnector({**base, "required_tags": {"team": "a"}})
    assert c.name_allowed("moks/x") and not c.name_allowed("prod/x") and not c.name_allowed("MOKS/x")
    assert c.tags_allowed({"team": "a", "x": "y"}) and not c.tags_allowed({"team": "b"})
    assert c.namespace_allowed("shop") and not c.namespace_allowed("kube-system")


def test_connector_namespaces_default_to_and_stay_inside_secrets_sync_namespaces(monkeypatch):
    from moks.connectors.base import ConnectorError

    base = {"account_id": ACCOUNT, "region": REGION, "allowed_prefixes": ["moks/"]}
    monkeypatch.setattr(get_settings(), "secrets_namespaces", "")
    with pytest.raises(ConnectorError):
        AwsSecretsConnector(base)
    monkeypatch.setattr(get_settings(), "secrets_namespaces", "shop, pay")
    c = AwsSecretsConnector(base)
    assert c.namespaces == ["shop", "pay"] and c.namespace_allowed("pay") and not c.namespace_allowed("other")
    narrow = AwsSecretsConnector({**base, "kubernetes_namespaces": ["shop", "other"]})
    assert narrow.namespace_allowed("shop") and not narrow.namespace_allowed("pay") and not narrow.namespace_allowed("other")


async def test_api_role_is_refused_by_the_connector():
    from moks.connectors.base import ConnectorError

    c = AwsSecretsConnector({"account_id": ACCOUNT, "region": REGION, "allowed_prefixes": ["moks/"], "kubernetes_namespaces": ["shop"]}, client=FakeSecretsManager(), sts=FakeSTS(ACCOUNT))
    assert get_settings().process_role == "api"
    with pytest.raises(ConnectorError) as e:
        await c.list_secrets(1)
    assert e.value.kind == "forbidden" and "executor" in e.value.message


# ---- integration --------------------------------------------------------------------------------------------


class World:
    def __init__(self, client, env: str, sm: FakeSecretsManager, clusters: dict[str, FakeSecretsKube]):
        self.client, self.env, self.sm, self.clusters = client, env, sm, clusters
        self.users: dict[str, str] = {}
        self.ids: dict[str, str] = {}
        self.responses: list[str] = []

    @property
    def east(self) -> FakeSecretsKube:
        return self.clusters["east"]

    async def as_(self, who: str) -> dict:
        return await login(self.client, self.users[who])

    async def call(self, who: str, method: str, path: str, body=None, expect: int | None = None):
        h = await self.as_(who)
        r = await self.client.request(method, f"/moks/api/secrets/{self.env}{path}", json=body, headers=h)
        self.responses.append(r.text)
        if expect is not None:
            assert r.status_code == expect, r.text
        return r

    async def discover(self, who="op", namespaces=("shop",), kube="east", aws=True):
        body = {"kube_connector_id": self.ids[kube], "namespaces": list(namespaces)} if kube else {}
        if aws:
            body["aws_connector_id"] = self.ids["aws"]
        await self.call(who, "POST", "/discover", body, expect=202)
        await run_jobs()

    async def mapping(self, name="db", ns="shop", kube="east", **kw) -> dict:
        body = {"kube_connector_id": self.ids[kube], "namespace": ns, "secret_name": name, "aws_connector_id": self.ids["aws"], **kw}
        return (await self.call("op", "POST", "/mappings", body, expect=201)).json()

    async def plan(self, mapping_ids, kind="sync", who="op", **kw) -> dict:
        r = await self.call(who, "POST", "/plans", {"kind": kind, "mapping_ids": mapping_ids, "reason": "rotate the database password", **kw}, expect=201)
        await run_jobs()
        return await self.get(r.json()["id"])

    async def get(self, plan_id, who="op") -> dict:
        return (await self.call(who, "GET", f"/plans/{plan_id}", expect=200)).json()

    async def resolve(self, plan, item, **patch) -> dict:
        return (await self.call("op", "PATCH", f"/plans/{plan['id']}/items/{item['id']}", {"version": plan["version"], **patch}, expect=200)).json()

    async def approve_and_run(self, plan, who="op", approver="ap", run=True, confirm=None) -> dict:
        p = (await self.call(who, "POST", f"/plans/{plan['id']}/submit", {"version": plan["version"]}, expect=200)).json()
        if p["status"] == "pending":
            p = (await self.call(approver, "POST", f"/plans/{plan['id']}/decision", {"approve": True, "plan_hash": p["plan_hash"], "comment": "looks right", "confirm": confirm}, expect=200)).json()
        p = (await self.call(who, "POST", f"/plans/{plan['id']}/execute", {"plan_hash": p["plan_hash"]}, expect=202)).json()
        if run:
            await run_jobs()
        return await self.get(plan["id"], who)

    def aws_content(self, name: str) -> Content:
        _, v = self.sm.current(name)
        return from_aws(AwsValue(string=v["string"], binary=v["binary"]), "moks-k8s-v1", {}, "Opaque")


async def run_jobs(concurrently: bool = False) -> None:
    """Runs queued secrets-class jobs as the executor does."""
    s = get_settings()
    old, s.process_role = s.process_role, "executor"
    try:
        while True:
            ids = []
            while (jid := await jobs.claim(f"test-exec-{uuid.uuid4().hex[:4]}", jobs.EXECUTOR_CLASS)):
                ids.append(jid)
                if not concurrently:
                    break
            if not ids:
                return
            await asyncio.gather(*(jobs.run_one(j) for j in ids))
    finally:
        s.process_role = old


@pytest.fixture
async def world(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "secrets_sync_enabled", True)
    monkeypatch.setattr(s, "secrets_digest_key", KEY.decode())
    monkeypatch.setattr(s, "secrets_lease_wait_seconds", 0.3)
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "kubernetes", FakeKubernetes)
    registry._cache.clear()
    sm = FakeSecretsManager(ACCOUNT, REGION)
    run = uuid.uuid4().hex[:6]
    clusters = {"east": FakeSecretsKube(f"uid-east-{run}"), "west": FakeSecretsKube(f"uid-west-{run}")}
    sts = FakeSTS(ACCOUNT)

    async def factory(kube_row, aws_row):
        kube = clusters[kube_row.name] if kube_row is not None else None
        aws = AwsSecretsConnector(aws_row.config or {}, aws_row.credential_ref, client=sm, sts=sts) if aws_row is not None else None
        return kube, aws

    monkeypatch.setattr(tasks, "backends_factory", factory)
    name = f"sec-{uuid.uuid4().hex[:6]}"
    executor = {"mode": "in_cluster", "executor": {"mode": "in_cluster"}}
    aws_cfg = {"account_id": ACCOUNT, "region": REGION, "allowed_prefixes": ["moks/", "import/"], "kubernetes_namespaces": ["shop", "pay"], "allow_delete": True, "recovery_window_days": 14}
    env_id = await make_env(name, connectors=[{"type": "kubernetes", "name": "east", "config": executor}, {"type": "kubernetes", "name": "west", "config": executor},
                                              {"type": "aws_secrets", "name": "aws", "config": aws_cfg}])
    w = World(client, name, sm, clusters)
    w.sts = sts
    async with session_scope() as db:
        from moks.models import Connector

        for c in (await db.execute(select(Connector).where(Connector.environment_id == env_id))).scalars().all():
            w.ids[c.name] = str(c.id)
    tag = uuid.uuid4().hex[:6]
    for who, grant in (("op", f"secrets_operator:{name}:*"), ("ap", f"secrets_approver:{name}:*"), ("op2", f"secrets_operator:{name}:*"),
                       ("shop_only", f"secrets_operator:{name}:shop"), ("viewer", f"secrets_viewer:{name}:shop"), ("nobody", f"viewer:{name}:*")):
        email = f"{who}-{tag}@example.com"
        await make_user(email, [grant])
        w.users[who] = email
    w.users["admin"] = "admin@example.com"
    await login(client, "admin@example.com")  # bootstrap admin exists after first sign-in
    yield w
    registry._cache.clear()


async def _export_one(w: World, data: dict[str, bytes] | None = None, name="db", ns="shop", **mapping) -> tuple[dict, dict]:
    w.east.put(ns, name, data or dict(MIXED.data))
    await w.discover(namespaces=(ns,))
    m = await w.mapping(name, ns, direction=mapping.pop("direction", "both"), **mapping)
    p = await w.plan([m["id"]])
    done = await w.approve_and_run(p)
    return m, done


async def test_export_import_and_unchanged_content(world):
    w = world
    m, done = await _export_one(w)
    assert done["status"] == "succeeded", done
    item = done["items"][0]
    assert item["classification"] == "missing_aws" and item["actions"] == {"aws": "create"} and item["status"] == "succeeded"
    aws_name = m["aws_secret_name"]
    assert aws_name == f"moks/{w.env}/east/shop/db"
    assert w.aws_content(aws_name) == MIXED
    assert w.sm.secrets[aws_name]["tags"]["moks:mapping"] == m["id"]
    async with session_scope() as db:
        row = await db.get(SecretMapping, uuid.UUID(m["id"]))
        assert row.last_status == "in_sync" and row.baseline["source"] == "verified_sync" and row.baseline_seq == 1
    versions_before = len(w.sm.secrets[aws_name]["versions"])

    # unchanged: the comparison finds nothing to do and no version is written
    again = await w.plan([m["id"]])
    assert again["items"][0]["classification"] == "in_sync" and again["items"][0]["status"] == "skipped"
    r = await w.call("op", "POST", f"/plans/{again['id']}/submit", {"version": again["version"]})
    assert r.status_code == 409 and "nothing to do" in r.text
    assert len(w.sm.secrets[aws_name]["versions"]) == versions_before

    # AWS changes (a key removed, one changed): import into Kubernetes
    changed = Content("Opaque", {"password": b"rotated", "bin": b"\x00\xff\xfe", "uni": "héllo ✓ 日本".encode()})
    w.sm.external_put(aws_name, to_aws(changed, "moks-k8s-v1", {}).string)
    imp = await w.plan([m["id"]])
    it = imp["items"][0]
    assert it["classification"] == "changed_aws" and it["proposal"] == "aws" and it["targets"] == ["kubernetes"]
    d = it["diff"]["kubernetes"]
    assert [x["key"] for x in d["removed"]] == ["empty"] and [x["key"] for x in d["changed"]] == ["password"]
    assert all(set(x["from"]) == {"fp", "empty", "binary"} for x in d["changed"])
    out = await w.approve_and_run(imp)
    assert out["status"] == "succeeded", out
    assert w.east.data("shop", "db") == dict(changed.data)


async def test_import_existing_aws_json_with_key_mapping(world):
    w = world
    w.sm.seed("import/billing", string=json.dumps({"API_KEY": "abc", "n": "", "ignored": "x"}))
    await w.discover(namespaces=("pay",))
    m = await w.mapping("billing", "pay", aws_secret_name="import/billing", payload_format="json", key_mapping={"keys": {"API_KEY": "api-key", "n": "empty"}}, direction="import")
    p = await w.plan([m["id"]])
    assert p["items"][0]["classification"] == "missing_kubernetes" and p["items"][0]["actions"] == {"kubernetes": "create"}
    done = await w.approve_and_run(p)
    assert done["status"] == "succeeded", done
    assert w.east.data("pay", "billing") == {"api-key": b"abc", "empty": b""}
    # an unsupported AWS payload is reported, not guessed at
    w.sm.external_put("import/billing", json.dumps({"API_KEY": 5}))
    bad = await w.plan([m["id"]])
    assert bad["items"][0]["classification"] == "unsupported" and "API_KEY" in bad["items"][0]["aws"]["unsupported_keys"]


async def test_same_secret_name_in_two_clusters_and_namespaces(world):
    w = world
    for c in ("east", "west"):
        for ns in ("shop", "pay"):
            w.clusters[c].put(ns, "db", {"p": f"{c}-{ns}".encode()})
    await w.discover(namespaces=("shop", "pay"), kube="east")
    await w.discover(namespaces=("shop", "pay"), kube="west", aws=False)
    inv = (await w.call("op", "GET", "/inventory?side=kubernetes", expect=200)).json()["items"]
    ids = [i["id"] for i in inv if i["name"] == "db"]
    assert len(ids) == 4
    drafts = (await w.call("op", "POST", "/mappings/propose", {"inventory_ids": ids, "aws_connector_id": w.ids["aws"]}, expect=200)).json()["items"]
    names = {d["aws_secret_name"] for d in drafts}
    assert names == {f"moks/{w.env}/{c}/{ns}/db" for c in ("east", "west") for ns in ("shop", "pay")}
    made = [await w.mapping("db", ns, kube=c, direction="export") for c in ("east", "west") for ns in ("shop", "pay")]
    p = await w.plan([m["id"] for m in made])
    done = await w.approve_and_run(p)
    assert done["status"] == "succeeded" and {i["status"] for i in done["items"]} == {"succeeded"}
    for c in ("east", "west"):
        for ns in ("shop", "pay"):
            assert w.aws_content(f"moks/{w.env}/{c}/{ns}/db").data == {"p": f"{c}-{ns}".encode()}
    # one AWS secret cannot be the target of two mappings
    w.east.put("shop", "other", {"p": b"x"})
    r = await w.call("op", "POST", "/mappings", {"kube_connector_id": w.ids["east"], "namespace": "shop", "secret_name": "other", "aws_connector_id": w.ids["aws"],
                                                 "aws_secret_name": f"moks/{w.env}/west/pay/db"})
    assert r.status_code == 409


async def test_aws_discovery_paginates_respects_bounds_and_reports_partial(world):
    w = world
    for i in range(230):
        w.sm.seed(f"moks/app/s{i:03d}", string="{}")
    w.sm.seed("MOKS/upper-case", string="{}")  # the AWS name filter is case-insensitive; the boundary is not
    w.sm.seed("prod/outside", string="{}")
    await w.discover(kube=None)
    inv = (await w.call("op", "GET", "/inventory?side=aws&limit=2000", expect=200)).json()
    names = {i["name"] for i in inv["items"]}
    assert len(names) == 230 and "MOKS/upper-case" not in names and "prod/outside" not in names
    assert inv["scopes"][0]["complete"] and w.sm.calls.count("list_secrets") == 3
    # page 2 denied: the scope says partial, keeps what it could not reach, and the API says why
    w.sm.deny_list_page = 2
    await w.discover(kube=None)
    inv = (await w.call("op", "GET", "/inventory?side=aws&limit=2000", expect=200)).json()
    assert not inv["scopes"][0]["complete"] and "IAM denied" in inv["scopes"][0]["error"]
    assert len(inv["items"]) == 230
    # a namespace-scoped operator cannot list the whole AWS account
    r = await w.call("shop_only", "POST", "/discover", {"aws_connector_id": w.ids["aws"]})
    assert r.status_code == 403


async def test_first_sync_conflict_needs_a_choice_and_edited_resolution(world):
    w = world
    w.east.put("shop", "app", {"a": b"k8s-a", "b": b"k8s-b"})
    w.sm.seed(f"moks/{w.env}/east/shop/app", string=to_aws(Content("Opaque", {"a": b"aws-a", "c": b"aws-c"}), "moks-k8s-v1", {}).string)
    await w.discover()
    m = await w.mapping("app", direction="both")
    p = await w.plan([m["id"]])
    it = p["items"][0]
    assert it["classification"] == "changed_both" and it["first_sync"] and it["status"] == "conflict" and it["proposal"] is None
    r = await w.call("op", "POST", f"/plans/{p['id']}/submit", {"version": p["version"]})
    assert r.status_code == 409 and "resolution" in r.text
    p = await w.resolve(p, it, resolution="edited", key_choices={"a": "aws", "b": "kubernetes"})
    assert p["items"][0]["status"] == "conflict"  # c has no choice yet
    p = await w.resolve(p, p["items"][0], resolution="edited", key_choices={"a": "aws", "b": "kubernetes", "c": "omit"})
    it = p["items"][0]
    assert it["status"] == "pending" and sorted(it["targets"]) == ["aws", "kubernetes"]
    done = await w.approve_and_run(p)
    assert done["status"] == "succeeded", done
    want = {"a": b"aws-a", "b": b"k8s-b"}
    assert w.east.data("shop", "app") == want and w.aws_content(m["aws_secret_name"]).data == want

    # both sides change differently after a verified sync: a conflict again, resolved by picking AWS
    w.east.put("shop", "app", {"a": b"k8s-new", "b": b"k8s-b"})
    w.sm.external_put(m["aws_secret_name"], to_aws(Content("Opaque", {"a": b"aws-new", "b": b"k8s-b"}), "moks-k8s-v1", {}).string)
    p = await w.plan([m["id"]])
    it = p["items"][0]
    assert it["classification"] == "changed_both" and not it["first_sync"]
    p = await w.resolve(p, it, resolution="aws")
    assert p["items"][0]["targets"] == ["kubernetes"]
    assert (await w.approve_and_run(p))["status"] == "succeeded"
    assert w.east.data("shop", "app")["a"] == b"aws-new"


async def test_a_change_after_approval_makes_the_item_stale(world):
    w = world
    w.east.put("shop", "db", {"p": b"one"})
    await w.discover()
    m = await w.mapping("db", direction="export")
    p = await w.plan([m["id"]])
    w.east.put("shop", "db", {"p": b"two"})  # after comparison and before execution
    done = await w.approve_and_run(p)
    assert done["status"] == "failed" and done["items"][0]["status"] == "stale"
    assert f"moks/{w.env}/east/shop/db" not in w.sm.secrets
    async with session_scope() as db:
        assert (await db.get(SecretMapping, uuid.UUID(m["id"]))).baseline is None
    r = await w.call("op", "POST", f"/plans/{p['id']}/retry", {})
    assert r.status_code == 409 and "stale" in r.text
    fresh = (await w.call("op", "POST", f"/plans/{p['id']}/refresh", expect=201)).json()
    await run_jobs()
    fresh = await w.get(fresh["id"])
    assert fresh["source_plan_id"] == p["id"] and fresh["status"] == "draft"
    assert (await w.approve_and_run(fresh))["status"] == "succeeded"
    assert w.aws_content(m["aws_secret_name"]).data == {"p": b"two"}


async def test_edits_after_submission_and_hash_tampering_are_refused(world):
    w = world
    w.east.put("shop", "db", {"p": b"one"})
    await w.discover()
    m = await w.mapping("db", direction="export")
    p = await w.plan([m["id"]])
    sub = (await w.call("op", "POST", f"/plans/{p['id']}/submit", {"version": p["version"]}, expect=200)).json()
    r = await w.call("op", "PATCH", f"/plans/{p['id']}/items/{p['items'][0]['id']}", {"version": sub["version"], "included": False})
    assert r.status_code == 409
    r = await w.call("op", "POST", f"/plans/{p['id']}/decision", {"approve": True, "plan_hash": sub["plan_hash"]})
    assert r.status_code == 403  # never one's own
    async with session_scope() as db:  # someone changes the stored plan behind the reviewer's back
        await db.execute(text("UPDATE secret_plan_items SET targets = '{kubernetes}' WHERE plan_id = :p"), {"p": uuid.UUID(p["id"])})
    r = await w.call("ap", "POST", f"/plans/{p['id']}/decision", {"approve": True, "plan_hash": sub["plan_hash"]})
    assert r.status_code == 409 and "hash" in r.text


async def test_mapping_edit_after_approval_and_optimistic_concurrency(world):
    w = world
    w.east.put("shop", "db", {"p": b"one"})
    await w.discover()
    m = await w.mapping("db", direction="export")
    p = await w.plan([m["id"]])
    r = await w.call("op", "PATCH", f"/mappings/{m['id']}", {"version": m["version"] + 5, "direction": "both"})
    assert r.status_code == 409
    await w.call("op", "PATCH", f"/mappings/{m['id']}", {"version": m["version"], "direction": "both"}, expect=200)
    done = await w.approve_and_run(p)
    assert done["items"][0]["status"] == "stale" and "mapping" in done["items"][0]["error"]


async def test_concurrent_operations_on_one_mapping(world):
    w = world
    w.east.put("shop", "db", {"p": b"one"})
    await w.discover()
    m = await w.mapping("db", direction="export")
    a = await w.plan([m["id"]])
    b = await w.plan([m["id"]], who="op2")
    for plan, who in ((a, "op"), (b, "op2")):
        await w.approve_and_run(plan, who=who, run=False)
    await run_jobs(concurrently=True)
    results = sorted([(await w.get(a["id"]))["items"][0]["status"], (await w.get(b["id"], "op2"))["items"][0]["status"]])
    assert results == ["stale", "succeeded"], results
    assert len(w.sm.secrets[m["aws_secret_name"]]["versions"]) == 1  # one create, no second write
    async with session_scope() as db:
        row = await db.get(SecretMapping, uuid.UUID(m["id"]))
        assert row.baseline_seq == 1 and row.lease_owner is None


async def test_an_ambiguous_write_is_resolved_on_retry_without_a_second_version(world):
    w = world
    m, _ = await _export_one(w, {"p": b"one"}, direction="export")
    name = m["aws_secret_name"]
    w.east.put("shop", "db", {"p": b"two"})
    p = await w.plan([m["id"]])
    w.sm.timeout_after.add("put_secret_value")  # the version is written; the response is lost
    done = await w.approve_and_run(p)
    it = done["items"][0]
    assert done["status"] == "failed" and it["status"] == "failed" and "not known" in it["error"] and it["write_state"]["aws"] == "intent"
    assert len(w.sm.secrets[name]["versions"]) == 2 and w.aws_content(name).data == {"p": b"one"}  # staged, not current
    async with session_scope() as db:
        assert (await db.get(SecretMapping, uuid.UUID(m["id"]))).baseline_seq == 1  # not advanced
    (await w.call("op", "POST", f"/plans/{p['id']}/retry", {}, expect=202))
    await run_jobs()
    done = await w.get(p["id"])
    assert done["status"] == "succeeded", done
    assert len(w.sm.secrets[name]["versions"]) == 2  # the same token found the same version
    assert w.aws_content(name).data == {"p": b"two"}
    staged = [v for v in w.sm.secrets[name]["versions"].values() if "MOKSPENDING" in v["stages"]]
    assert not staged

    # the swap landed but its response was lost: the retry sees the destination already holds it and writes nothing
    w.east.put("shop", "db", {"p": b"three"})
    p = await w.plan([m["id"]])
    w.sm.timeout_after.add("update_secret_version_stage")
    first = await w.approve_and_run(p)
    assert first["items"][0]["status"] == "failed"
    puts = w.sm.calls.count("put_secret_value")
    await w.call("op", "POST", f"/plans/{p['id']}/retry", {}, expect=202)
    await run_jobs()
    assert (await w.get(p["id"]))["status"] == "succeeded"
    assert w.sm.calls.count("put_secret_value") == puts and w.aws_content(name).data == {"p": b"three"}


async def test_an_outside_write_between_stage_and_swap_is_not_overwritten(world):
    w = world
    m, _ = await _export_one(w, {"p": b"one"}, direction="export")
    w.east.put("shop", "db", {"p": b"two"})
    p = await w.plan([m["id"]])
    w.sm.hooks["update_secret_version_stage"] = lambda: w.sm.external_put(m["aws_secret_name"], to_aws(Content("Opaque", {"p": b"outside"}), "moks-k8s-v1", {}).string)
    done = await w.approve_and_run(p)
    assert done["items"][0]["status"] == "stale"
    assert w.aws_content(m["aws_secret_name"]).data == {"p": b"outside"}
    assert not [v for v in w.sm.secrets[m["aws_secret_name"]]["versions"].values() if "MOKSPENDING" in v["stages"]]


async def test_partial_bulk_failure_keeps_baselines_correct_and_retries(world):
    w = world
    for n in ("a", "b", "c"):
        w.east.put("shop", n, {"p": n.encode()})
    await w.discover()
    ms = [await w.mapping(n, direction="export") for n in ("a", "b", "c")]
    p = await w.plan([m["id"] for m in ms])
    calls = {"n": 0}

    def kms_on_second():
        calls["n"] += 1
        if calls["n"] == 2:
            w.sm.deny_once["create_secret"] = client_error("EncryptionFailure", "KMS key is disabled")

    w.sm.hooks["create_secret"] = kms_on_second
    orig = w.sm.create_secret

    def create(**kw):
        if "create_secret" not in w.sm.hooks:
            w.sm.hooks["create_secret"] = kms_on_second
        return orig(**kw)

    w.sm.create_secret = create
    done = await w.approve_and_run(p)
    statuses = [i["status"] for i in done["items"]]
    assert done["status"] == "partial" and statuses == ["succeeded", "failed", "succeeded"], statuses
    assert "KMS" in done["items"][1]["error"]
    async with session_scope() as db:
        seqs = [(await db.get(SecretMapping, uuid.UUID(m["id"]))).baseline_seq for m in ms]
    assert seqs == [1, 0, 1]
    w.sm.create_secret = orig
    w.sm.hooks.clear()
    await w.call("op", "POST", f"/plans/{p['id']}/retry", {}, expect=202)
    await run_jobs()
    done = await w.get(p["id"])
    assert done["status"] == "succeeded" and [i["status"] for i in done["items"]] == ["succeeded"] * 3


async def test_controller_owned_secrets_need_an_explicit_override(world):
    w = world
    w.east.put("shop", "eso", {"p": b"x"}, ownerReferences=[{"kind": "ExternalSecret", "name": "eso", "apiVersion": "external-secrets.io/v1"}])
    w.east.put("shop", "token", {"token": b"t"}, "kubernetes.io/service-account-token")
    w.sm.seed(f"moks/{w.env}/east/shop/eso", string=to_aws(Content("Opaque", {"p": b"from-aws"}), "moks-k8s-v1", {}).string)
    await w.discover()
    inv = {i["name"]: i for i in (await w.call("op", "GET", "/inventory?side=kubernetes", expect=200)).json()["items"]}
    assert inv["eso"]["owner"]["manager"] == "external-secrets" and inv["token"]["excluded"] and inv["token"]["sync_status"] == "excluded"
    r = await w.call("op", "POST", "/mappings", {"kube_connector_id": w.ids["east"], "namespace": "shop", "secret_name": "token", "aws_connector_id": w.ids["aws"]})
    assert r.status_code == 422 and "service-account" in r.text
    m = await w.mapping("eso", direction="import")
    p = await w.plan([m["id"]])
    p = await w.resolve(p, p["items"][0], resolution="aws")
    it = p["items"][0]
    assert it["status"] == "skipped" and "external-secrets manages" in it["blocked"]
    m = (await w.call("op", "PATCH", f"/mappings/{m['id']}", {"version": m["version"], "management_policy": "override",
                                                              "expected_owner": {"kubernetes": "external-secrets", "aws": "none"}}, expect=200)).json()
    p = await w.plan([m["id"]])
    p = await w.resolve(p, p["items"][0], resolution="aws")
    it = p["items"][0]
    assert it["status"] == "pending" and any("external-secrets manages" in x for x in it["warnings"])
    assert (await w.approve_and_run(p))["status"] == "succeeded"


async def test_iam_kms_identity_and_namespace_denials(world):
    w = world
    w.east.put("shop", "db", {"p": b"one"})
    w.east.put("pay", "db", {"p": b"one"})
    await w.discover(namespaces=("shop", "pay"))
    m = await w.mapping("db", direction="export")
    w.sm.deny["describe_secret"] = "AccessDeniedException"
    p = await w.plan([m["id"]])
    it = p["items"][0]
    assert it["classification"] == "failed" and "IAM denied" in it["aws"]["error"]
    w.sm.deny.clear()
    w.sm.seed(m["aws_secret_name"], string=to_aws(Content("Opaque", {"p": b"one"}), "moks-k8s-v1", {}).string)
    w.sm.deny["get_secret_value"] = "KMS"
    it = (await w.plan([m["id"]]))["items"][0]
    assert it["classification"] == "failed" and "KMS" in it["aws"]["error"]
    w.sm.deny.clear()
    w.east.denied_namespaces.add("shop")
    it = (await w.plan([m["id"]]))["items"][0]
    assert it["classification"] == "failed" and "RBAC denied" in it["kubernetes"]["error"]
    w.east.denied_namespaces.clear()
    w.sts.account = "999999999999"  # credentials for the wrong account
    it = (await w.plan([m["id"]]))["items"][0]
    assert it["classification"] == "failed" and "999999999999" in it["aws"]["error"]
    w.sts.account = ACCOUNT

    # MOKS RBAC and connector boundaries
    r = await w.call("shop_only", "POST", "/mappings", {"kube_connector_id": w.ids["east"], "namespace": "pay", "secret_name": "db", "aws_connector_id": w.ids["aws"]})
    assert r.status_code == 403
    inv = (await w.call("shop_only", "GET", "/inventory?side=kubernetes", expect=200)).json()
    assert {i["namespace"] for i in inv["items"]} == {"shop"} and inv["hidden"] >= 1
    assert (await w.call("viewer", "GET", "/inventory?side=kubernetes", expect=200)).json()["items"][0]["keys"] == ["p"]
    assert (await w.call("nobody", "GET", "/inventory")).status_code == 403
    r = await w.call("viewer", "POST", "/plans", {"mapping_ids": [m["id"]], "reason": "should not work"})
    assert r.status_code == 403
    w.east.put("other", "db", {"p": b"x"})
    r = await w.call("op", "POST", "/mappings", {"kube_connector_id": w.ids["east"], "namespace": "other", "secret_name": "db", "aws_connector_id": w.ids["aws"]})
    assert r.status_code == 403 and "kubernetes_namespaces" in r.text
    r = await w.call("op", "POST", "/mappings", {"kube_connector_id": w.ids["east"], "namespace": "pay", "secret_name": "db", "aws_connector_id": w.ids["aws"], "aws_secret_name": "prod/db"})
    assert r.status_code == 422


async def test_deletion_needs_someone_elses_approval_and_respects_recovery(world):
    w = world
    m, _ = await _export_one(w, {"p": b"one"}, direction="export")
    p = await w.plan([m["id"]], kind="delete", who="admin", targets=["aws"])
    assert p["items"][0]["actions"] == {"aws": "delete"}
    sub = (await w.call("admin", "POST", f"/plans/{p['id']}/submit", {"version": p["version"]}, expect=200)).json()
    assert sub["status"] == "pending" and sub["requires_approval"]  # an admin's own deletion still needs someone else
    r = await w.call("admin", "POST", f"/plans/{p['id']}/decision", {"approve": True, "plan_hash": sub["plan_hash"], "confirm": "delete 1 secret"})
    assert r.status_code == 403
    r = await w.call("ap", "POST", f"/plans/{p['id']}/decision", {"approve": True, "plan_hash": sub["plan_hash"], "confirm": "yes"})
    assert r.status_code == 422
    await w.call("ap", "POST", f"/plans/{p['id']}/decision", {"approve": True, "plan_hash": sub["plan_hash"], "confirm": "delete 1 secret"}, expect=200)
    await w.call("admin", "POST", f"/plans/{p['id']}/execute", {"plan_hash": sub["plan_hash"]}, expect=202)
    await run_jobs()
    assert (await w.get(p["id"], "admin"))["status"] == "succeeded"
    s = w.sm.secrets[m["aws_secret_name"]]
    assert s["deleted"] is not None and s["recovery_days"] == 14
    # default behaviour afterwards: the missing side is reported, and recreation is a reviewed plan
    it = (await w.plan([m["id"]]))["items"][0]
    assert it["classification"] == "unsupported" and "scheduled for deletion" in (it["aws"].get("unsupported") or "")


async def test_admin_sync_plans_skip_the_second_approval_and_kubernetes_recreation(world):
    w = world
    m, _ = await _export_one(w, {"p": b"one"}, direction="both")
    del w.east.objects[("shop", "db")]
    p = await w.plan([m["id"]], who="admin")
    it = p["items"][0]
    assert it["classification"] == "missing_kubernetes" and it["actions"] == {"kubernetes": "create"}
    sub = (await w.call("admin", "POST", f"/plans/{p['id']}/submit", {"version": p["version"]}, expect=200)).json()
    assert sub["status"] == "approved" and not sub["requires_approval"]
    await w.call("admin", "POST", f"/plans/{p['id']}/execute", {"plan_hash": sub["plan_hash"]}, expect=202)
    await run_jobs()
    assert (await w.get(p["id"], "admin"))["status"] == "succeeded" and w.east.data("shop", "db") == {"p": b"one"}


async def test_rollback_to_a_prior_aws_version(world):
    w = world
    m, _ = await _export_one(w, {"p": b"one"}, direction="both")
    w.east.put("shop", "db", {"p": b"two"})
    p = await w.plan([m["id"]])
    assert (await w.approve_and_run(p))["status"] == "succeeded"
    hist = (await w.call("op", "GET", f"/history?mapping_id={m['id']}", expect=200)).json()["items"]
    last = hist[0]
    assert last["rollback_available"] and last["keys"] == ["p"] and last["aws_version_before"]
    versions = len(w.sm.secrets[m["aws_secret_name"]]["versions"])
    rb = await w.plan([m["id"]], kind="rollback", rollback={m["id"]: {"version": last["aws_version_before"], "targets": ["aws", "kubernetes"]}})
    it = rb["items"][0]
    assert it["actions"] == {"aws": "restage", "kubernetes": "update"} and it["rollback_version"] == last["aws_version_before"]
    done = await w.approve_and_run(rb)
    assert done["status"] == "succeeded", done
    assert w.aws_content(m["aws_secret_name"]).data == {"p": b"one"} and w.east.data("shop", "db") == {"p": b"one"}
    assert len(w.sm.secrets[m["aws_secret_name"]]["versions"]) == versions  # restaged, nothing new written


async def test_restart_is_a_separate_reviewed_action(world):
    from tests.fakes import reset_deployments

    reset_deployments()
    w = world
    w.east.workload_specs["shop"] = [{"kind": "Deployment", "name": "checkout", "pod_spec": {"containers": [{"envFrom": [{"secretRef": {"name": "db"}}]}]}}]
    m, _ = await _export_one(w, {"p": b"one"}, direction="both")
    w.sm.external_put(m["aws_secret_name"], to_aws(Content("Opaque", {"p": b"two"}), "moks-k8s-v1", {}).string)
    p = await w.plan([m["id"]])
    it = p["items"][0]
    assert it["impact"] == [{"kind": "Deployment", "name": "checkout", "how": ["envFrom"], "keys": [], "restart_required": True,
                             "note": "environment variables are read at container start: restart to pick up the change", "restartable": True}]
    path = f"/plans/{p['id']}/items/{it['id']}/restart"
    assert (await w.call("op", "POST", path, {"name": "nope", "reason": "pick up the new password"})).status_code == 422
    # the restart is judged by the action rules: a secrets operator holds no action.request
    assert (await w.call("op", "POST", path, {"name": "checkout", "reason": "pick up the new password"})).status_code == 403
    r = await w.call("admin", "POST", path, {"name": "checkout", "reason": "pick up the new password"}, expect=201)
    async with session_scope() as db:
        from moks.models import ActionPlan

        ap = await db.get(ActionPlan, uuid.UUID(r.json()["action_plan_id"]))
        assert ap.action == "restart_deployment" and ap.status == "pending" and ap.namespace == "shop"
    assert ("replace", "shop", "db") not in w.east.writes  # the sync itself has not run, and proposing a restart wrote nothing


async def test_stopped_executor_leaves_a_retryable_plan(world):
    w = world
    w.east.put("shop", "db", {"p": b"one"})
    await w.discover()
    m = await w.mapping("db", direction="export")
    p = await w.plan([m["id"]])
    sub = (await w.call("op", "POST", f"/plans/{p['id']}/submit", {"version": p["version"]}, expect=200)).json()
    await w.call("ap", "POST", f"/plans/{p['id']}/decision", {"approve": True, "plan_hash": sub["plan_hash"]}, expect=200)
    await w.call("op", "POST", f"/plans/{p['id']}/execute", {"plan_hash": sub["plan_hash"]}, expect=202)
    async with session_scope() as db:  # the job died and was reaped; an item was mid-write
        await db.execute(text("UPDATE jobs SET status = 'failed' WHERE type = 'secret_execute' AND payload->>'plan_id' = :p"), {"p": p["id"]})
        await db.execute(text("UPDATE secret_plan_items SET status = 'running' WHERE plan_id = :p"), {"p": uuid.UUID(p["id"])})
        await db.execute(text("UPDATE secret_plans SET queued_at = now() - interval '5 minutes' WHERE id = :p"), {"p": uuid.UUID(p["id"])})
    assert await tasks.reconcile_orphans() == 1
    got = await w.get(p["id"])
    assert got["status"] == "failed" and got["items"][0]["status"] == "failed" and "not known" in got["items"][0]["error"]
    await w.call("op", "POST", f"/plans/{p['id']}/retry", {}, expect=202)
    await run_jobs()
    assert (await w.get(p["id"]))["status"] == "succeeded"


async def test_secret_values_stay_out_of_storage_logs_traces_errors_and_responses(world, caplog, monkeypatch):
    w = world
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(observability, "tracer", lambda: provider.get_tracer("moks"))
    caplog.set_level(logging.DEBUG)
    secret = {"password": SENTINEL.encode(), "pin": b"pin-4471-zzq"}
    m, done = await _export_one(w, secret, direction="both")
    assert done["status"] == "succeeded"
    # failures with values in flight: a KMS error, a timeout, a format refusal
    w.east.put("shop", "db", {"password": (SENTINEL + "-2").encode(), "bin": b"\xff"})
    p = await w.plan([m["id"]])
    w.sm.deny_once["put_secret_value"] = client_error("EncryptionFailure", "KMS refused")
    await w.approve_and_run(p)
    w.sm.external_put(m["aws_secret_name"], json.dumps({"format": "moks.k8s-secret/v1", "type": "Opaque", "data": {"password": "!!notbase64-" + SENTINEL}}))
    await w.plan([m["id"]])
    await w.call("op", "GET", f"/history?mapping_id={m['id']}", expect=200)
    await w.call("op", "GET", "/inventory?side=kubernetes", expect=200)

    forms = {SENTINEL, base64.b64encode(SENTINEL.encode()).decode(), hashlib.sha256(SENTINEL.encode()).hexdigest(),
             base64.b64encode((SENTINEL + "-2").encode()).decode()[:20], "pin-4471-zzq", base64.b64encode(b"pin-4471-zzq").decode(),
             hashlib.sha256(b"pin-4471-zzq").hexdigest()}
    async with session_scope() as db:
        dump = []
        for t in Base.metadata.tables:
            dump += [r[0] for r in (await db.execute(text(f"SELECT row_to_json(x)::text FROM {t} x"))).all()]
    blob = "\n".join(dump)
    for f in forms:
        assert f not in blob, f"value form found in the database: {f[:12]}"
        assert f not in caplog.text
        assert all(f not in r for r in w.responses)
    for s in exporter.get_finished_spans():
        text_of = json.dumps({"name": s.name, "attrs": dict(s.attributes or {}), "events": [e.name for e in s.events], "status": s.status.description}, default=str)
        assert not any(f in text_of for f in forms)
    assert len(dump) > 50 and any("secrets.item_execute" in r for r in dump)  # the dump really covered the run


async def test_the_feature_is_off_by_default_and_outside_llm_tools(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "secrets_sync_enabled", False)
    await login(client, "admin@example.com")
    assert (await client.get("/moks/api/secrets/config")).json() == {"enabled": False, "environments": []}
    assert (await client.get("/moks/api/secrets/x/inventory")).status_code == 404
    from moks.tools import builtin, executor as tool_executor, phase2

    names = " ".join(dir(builtin) + dir(phase2) + dir(tool_executor)).lower()
    assert "secret" not in "".join(n for n in names.split() if not n.startswith("_") and n not in ("redact",)).replace("secretkeyref", "")
    import moks.crew  # noqa: F401
    import sys

    tool_modules = [m for name, m in sys.modules.items() if name.startswith(("moks.tools", "moks.crew", "moks.orchestrator", "moks.llm")) and m]
    assert all("moks.secrets" not in (getattr(m, "__dict__", {}).get("__name__", "")) for m in tool_modules)
    assert not [name for name, m in sys.modules.items() if name.startswith(("moks.tools", "moks.crew", "moks.orchestrator", "moks.llm"))
                and any(getattr(v, "__module__", "").startswith("moks.secrets") for v in vars(m).values() if callable(v))]


async def test_workers_never_claim_or_run_secret_jobs():
    assert jobs.EXECUTOR_CLASS not in jobs.CLASSES
    assert jobs.job_class("secret_execute") == jobs.EXECUTOR_CLASS
    with pytest.raises(RuntimeError):
        await jobs.handlers()["secret_execute"](None, {"_job_type": "secret_execute", "plan_id": str(uuid.uuid4())})


async def _kube_names(w: World) -> set[tuple[str, str]]:
    inv = (await w.call("op", "GET", "/inventory?side=kubernetes", expect=200)).json()
    return {(i["namespace"], i["name"]) for i in inv["items"]}


async def test_discover_without_namespaces_covers_every_secrets_sync_namespace(world, monkeypatch):
    w = world
    monkeypatch.setattr(get_settings(), "secrets_namespaces", "shop,pay")
    w.east.put("shop", "a", {"p": b"1"})
    w.east.put("pay", "b", {"p": b"2"})
    w.east.put("other", "c", {"p": b"3"})
    await w.discover(namespaces=())
    assert {("shop", "a"), ("pay", "b")} <= await _kube_names(w) and not any(ns == "other" for ns, _ in await _kube_names(w))


async def test_executor_discovers_secrets_sync_namespaces_on_its_own(world, monkeypatch):
    w = world
    s = get_settings()
    monkeypatch.setattr(s, "secrets_namespaces", "shop,pay")
    monkeypatch.setattr(s, "secrets_discovery_interval_minutes", 0)
    monkeypatch.setattr(tasks, "_last_auto", None)
    assert await tasks.schedule_discovery() == 0  # off

    monkeypatch.setattr(s, "secrets_discovery_interval_minutes", 60)
    w.east.put("shop", "auto", {"p": b"1"})
    w.clusters["west"].put("pay", "auto", {"p": b"2"})

    async def mine() -> list[dict]:
        async with session_scope() as db:
            rows = (await db.execute(text("SELECT payload FROM jobs WHERE type = 'secret_discover' AND status = 'queued' AND payload->>'environment_id' = :e"),
                                     {"e": await _env_id(w.env)})).scalars().all()
        return list(rows)

    await tasks.schedule_discovery()
    queued = await mine()
    assert sorted(bool(p.get("kube_connector_id")) for p in queued) == [False, True, True]  # east, west, and AWS
    assert all(p["namespaces"] == ["shop", "pay"] for p in queued if p.get("kube_connector_id"))
    assert all(p["requested_by"] is None and p["trigger"] == "schedule" for p in queued)
    await tasks.schedule_discovery()  # within the interval: nothing new
    monkeypatch.setattr(tasks, "_last_auto", None)
    await tasks.schedule_discovery()  # due again, but the same listings are still queued
    assert len(await mine()) == 3

    await run_jobs()
    assert {("shop", "auto"), ("pay", "auto")} <= await _kube_names(w)
    inv = (await w.call("op", "GET", "/inventory?side=aws", expect=200)).json()
    assert inv["scopes"]


async def _env_id(name: str) -> str:
    from moks.models import Environment

    async with session_scope() as db:
        return str((await db.execute(select(Environment.id).where(Environment.name == name))).scalar_one())


# ---- bulk mapping ---------------------------------------------------------------------------------------------


async def _inventory(w: World, side="kubernetes") -> list[dict]:
    return (await w.call("op", "GET", f"/inventory?side={side}", expect=200)).json()["items"]


async def test_bulk_map_exports_kubernetes_secrets_and_imports_aws_ones(world):
    w = world
    w.east.put("shop", "a", {"p": b"1"})
    w.east.put("shop", "b", {"p": b"2"})
    w.sm.seed("import/appconfig", string=json.dumps({"url": "https://x"}))
    await w.discover()
    kube = {r["name"]: r["id"] for r in await _inventory(w)}
    r = (await w.call("op", "POST", "/mappings/bulk", {"inventory_ids": [kube["a"], kube["b"]], "aws_connector_id": w.ids["aws"]}, expect=201)).json()
    assert sorted(m["secret_name"] for m in r["created"]) == ["a", "b"] and not r["skipped"]
    assert {m["direction"] for m in r["created"]} == {"export"} and {m["aws_secret_name"] for m in r["created"]} == {f"moks/{w.env}/east/shop/a", f"moks/{w.env}/east/shop/b"}
    await run_jobs()
    plan = await w.get(r["plan_id"])
    assert plan["status"] == "draft" and {i["classification"] for i in plan["items"]} == {"missing_aws"}
    again = (await w.call("op", "POST", "/mappings/bulk", {"inventory_ids": [kube["a"]], "aws_connector_id": w.ids["aws"], "compare": False}, expect=201)).json()
    assert not again["created"] and "already uses" in again["skipped"][0]["why"]  # mapped once only

    aws = {r["name"]: r["id"] for r in await _inventory(w, "aws")}
    r = (await w.call("op", "POST", "/mappings/bulk", {"inventory_ids": [aws["import/appconfig"]], "aws_connector_id": w.ids["aws"], "compare": False}, expect=201)).json()
    assert not r["created"] and "namespace" in r["skipped"][0]["why"]
    r = (await w.call("op", "POST", "/mappings/bulk", {"inventory_ids": [aws["import/appconfig"]], "aws_connector_id": w.ids["aws"], "namespace": "pay",
                                                       "kube_connector_id": w.ids["east"], "compare": False}, expect=201)).json()
    assert r["created"], r["skipped"]
    m = r["created"][0]
    assert (m["namespace"], m["secret_name"], m["direction"], m["payload_format"]) == ("pay", "appconfig", "import", "json")
    r = await w.call("viewer", "POST", "/mappings/bulk", {"inventory_ids": [kube["b"]], "aws_connector_id": w.ids["aws"]})
    assert r.status_code == 403


# ---- schedules ------------------------------------------------------------------------------------------------


async def run_schedules() -> list[dict]:
    """Runs due schedules as the executor loop does."""
    s = get_settings()
    old, s.process_role = s.process_role, "executor"
    out = []
    try:
        while (sid := await tasks.due_schedule()) is not None:
            out.append(await tasks.run_schedule(sid))
    finally:
        s.process_role = old
    return out


async def test_an_approved_schedule_syncs_one_sided_changes_and_leaves_conflicts(world):
    w = world
    m, done = await _export_one(w, {"p": b"one"}, direction="both")
    assert done["status"] == "succeeded"
    s = (await w.call("op", "POST", "/schedules", {"name": "shop db", "mapping_ids": [m["id"]], "interval_minutes": 60}, expect=201)).json()
    assert s["status"] == "pending" and not s["allowed"]["approve"]
    assert await run_schedules() == []  # nothing runs before approval
    r = await w.call("op", "POST", f"/schedules/{s['id']}/decision", {"approve": True, "schedule_hash": s["schedule_hash"]})
    assert r.status_code == 403  # never one's own
    assert (await w.call("viewer", "POST", f"/schedules/{s['id']}/decision", {"approve": True, "schedule_hash": s["schedule_hash"]})).status_code == 403
    s = (await w.call("ap", "POST", f"/schedules/{s['id']}/decision", {"approve": True, "schedule_hash": s["schedule_hash"], "comment": "ok"}, expect=200)).json()
    assert s["status"] == "active"

    w.east.put("shop", "db", {"p": b"two"})  # changed in Kubernetes only
    (run,) = await run_schedules()
    assert run["status"] == "succeeded", run
    assert w.aws_content(m["aws_secret_name"]).data == {"p": b"two"}
    s = (await w.call("op", "GET", "/schedules", expect=200)).json()["items"][0]
    assert s["last_outcome"]["written"] == 1 and not s["last_outcome"]["needs_review"]
    plan = await w.get(s["last_plan_id"])
    assert plan["summary"]["schedule"] == "shop db" and plan["requested_by"] == w.users["op"]
    assert await run_schedules() == []  # not due again for an hour

    versions = len(w.sm.secrets[m["aws_secret_name"]]["versions"])
    w.east.put("shop", "db", {"p": b"three"})
    w.sm.external_put(m["aws_secret_name"], to_aws(Content("Opaque", {"p": b"four"}), "moks-k8s-v1", {}).string)
    await w.call("op", "POST", f"/schedules/{s['id']}/run", expect=200)
    (run,) = await run_schedules()
    s = (await w.call("op", "GET", "/schedules", expect=200)).json()["items"][0]
    assert s["last_outcome"]["written"] == 0 and s["last_outcome"]["needs_review"][0]["classification"] == "changed_both"
    assert w.east.data("shop", "db") == {"p": b"three"} and len(w.sm.secrets[m["aws_secret_name"]]["versions"]) == versions + 1  # only the outside write

    # editing the mapping voids the approval: the next run pauses instead of writing
    await w.call("op", "PATCH", f"/mappings/{m['id']}", {"version": m["version"], "direction": "export"}, expect=200)
    await w.call("op", "POST", f"/schedules/{s['id']}/run", expect=200)
    (run,) = await run_schedules()
    assert "edited after" in run["paused"]
    s = (await w.call("op", "GET", "/schedules", expect=200)).json()["items"][0]
    assert s["status"] == "paused" and "edited after" in s["paused_reason"] and s["stale_mappings"] == ["shop/db"]
    s = (await w.call("op", "PATCH", f"/schedules/{s['id']}", {"version": s["version"], "refresh": True}, expect=200)).json()
    assert s["status"] == "pending" and s["stale_mappings"] == []


async def test_a_run_rechecks_the_approver_before_writing(world):
    w = world
    m, _ = await _export_one(w, {"p": b"one"}, direction="export")
    s = (await w.call("op", "POST", "/schedules", {"name": "check", "mapping_ids": [m["id"]], "interval_minutes": 15}, expect=201)).json()
    await w.call("ap", "POST", f"/schedules/{s['id']}/decision", {"approve": True, "schedule_hash": s["schedule_hash"]}, expect=200)
    async with session_scope() as db:  # the approver loses the role
        await db.execute(text("DELETE FROM role_bindings WHERE user_id = (SELECT id FROM users WHERE email = :e)"), {"e": w.users["ap"]})
    w.east.put("shop", "db", {"p": b"two"})
    (run,) = await run_schedules()
    assert "approval is no longer valid" in run["paused"]
    assert w.aws_content(m["aws_secret_name"]).data == {"p": b"one"}


# ---- viewing values -------------------------------------------------------------------------------------------


def _browser_key():
    from cryptography.hazmat.primitives.asymmetric import ec

    from moks.secrets.seal import _b64

    private = ec.generate_private_key(ec.SECP256R1())
    n = private.public_key().public_numbers()
    return private, {"kty": "EC", "crv": "P-256", "x": _b64(n.x.to_bytes(32, "big")), "y": _b64(n.y.to_bytes(32, "big"))}


async def test_values_are_sealed_to_the_browser_shown_once_and_audited(world):
    from moks.secrets.seal import open_sealed

    w = world
    w.east.put("shop", "db", {"password": SENTINEL.encode(), "blob": b"\x00\xff"})
    await w.discover()
    rid = next(r["id"] for r in await _inventory(w) if r["name"] == "db")
    private, jwk = _browser_key()
    assert (await w.call("op", "POST", "/reveal", {"inventory_id": rid, "public_key": jwk})).status_code == 403  # operators cannot
    assert (await w.call("ap", "POST", "/reveal", {"inventory_id": rid, "public_key": {**jwk, "d": "x"}})).status_code == 422  # never a private key
    job = (await w.call("ap", "POST", "/reveal", {"inventory_id": rid, "public_key": jwk}, expect=202)).json()["job_id"]
    assert (await w.call("ap", "GET", f"/reveal/{job}", expect=200)).json()["status"] == "queued"
    await run_jobs()
    async with session_scope() as db:  # what the database holds is ciphertext
        stored = (await db.execute(text("SELECT payload::text || result::text FROM jobs WHERE id = :j"), {"j": uuid.UUID(job)})).scalar_one()
    assert SENTINEL not in stored and base64.b64encode(SENTINEL.encode()).decode() not in stored
    r = (await w.call("ap", "GET", f"/reveal/{job}", expect=200)).json()
    doc = json.loads(open_sealed(private, r["sealed"], job))
    assert doc["keys"] == {"blob": {"base64": "AP8="}, "password": {"text": SENTINEL}} and doc["type"] == "Opaque"
    assert (await w.call("ap", "GET", f"/reveal/{job}")).status_code == 410  # once
    assert (await w.call("admin", "GET", f"/reveal/{job}")).status_code == 404  # only to the person who asked
    async with session_scope() as db:
        actions = set((await db.execute(text("SELECT action FROM audit_events WHERE resource_id = 'kubernetes:shop/db'"))).scalars().all())
    assert {"secrets.reveal_request", "secrets.reveal"} <= actions
    shop_only = (await w.call("viewer", "POST", "/reveal", {"inventory_id": rid, "public_key": jwk})).status_code
    assert shop_only == 403


async def test_the_inventory_filters_by_several_types_and_lists_the_types_found(world):
    w = world
    w.east.put("shop", "plain", {"p": b"1"})
    w.east.put("shop", "cert", {"tls.crt": b"c", "tls.key": b"k"}, "kubernetes.io/tls")
    w.east.put("shop", "login", {"username": b"u", "password": b"p"}, "kubernetes.io/basic-auth")
    await w.discover()
    r = (await w.call("op", "GET", "/inventory?side=kubernetes&type=kubernetes.io/tls&type=Opaque", expect=200)).json()
    assert {i["name"] for i in r["items"]} >= {"plain", "cert"} and "login" not in {i["name"] for i in r["items"]}
    assert {"Opaque", "kubernetes.io/tls", "kubernetes.io/basic-auth"} <= set(r["types"])
