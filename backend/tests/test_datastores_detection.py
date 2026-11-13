"""Data-store clusters in detection, investigations, the crew and the Data page."""
from __future__ import annotations

import uuid

import pytest

from moks import datastores, detection, skills
from moks.api.environments import sync_automation_binding
from moks.connectors import registry
from moks.connectors.base import ConnectorError
from moks.crew.tools import ALLOWED_MOKS_TOOLS, BY_NAME, Target, bind
from moks.db import session_scope
from moks.models import Environment
from moks.tools.builtin import REGISTRY
from tests.conftest import login, make_env

STATE = {"connections": 99.0, "down": set()}


class FakePg:
    type = "postgres"

    def __init__(self, config, credential_ref=None):
        self.host = config["host"]

    async def aclose(self):
        pass

    async def test(self):
        return "fake"

    async def overview(self):
        if self.host in STATE["down"]:
            raise ConnectorError("unavailable", f"cannot reach postgres at {self.host}:5432")
        return {"server": {"role": "primary", "version": "PostgreSQL 17"}, "connections": {"total": 99, "max": 100, "used_pct": STATE["connections"], "by_source": []},
                "blocked_sessions": 0, "replication": {"role": "primary", "replicas": [], "slots": []}}

    async def settings(self):
        return []

    async def locks(self):
        return {"blocked": [], "root_blockers": [], "waiting_on": []}


@pytest.fixture(autouse=True)
def fake_pg(monkeypatch):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "postgres", FakePg)
    registry._cache.clear()
    STATE.update(connections=99.0, down=set())
    yield
    registry._cache.clear()


async def _env(clusters):
    name = f"dsd-{uuid.uuid4().hex[:6]}"
    env_id = await make_env(name, connectors=[{"type": "postgres", "name": n, "config": {"host": n, "namespace": ns}} for n, ns in clusters])
    async with session_scope() as db:
        await sync_automation_binding(db, name, True)
    return env_id, name


def _spec(**kw):
    return skills.DetectionSpec(id=f"r-{uuid.uuid4().hex[:6]}", kind="datastore", finding="pg_connections", **kw)


async def _evaluate(env_id, spec):
    async with session_scope() as db:
        env = await db.get(Environment, env_id)
        return await detection.evaluate(db, env, spec)


async def test_detection_fires_per_cluster_and_recovers_only_what_it_read():
    env_id, _ = await _env([("shop-db", "shop"), ("pay-db", "payments")])
    check = await _evaluate(env_id, _spec())
    assert check.status == "firing" and check.complete
    assert sorted((s.namespace, s.resource) for s in check.signals) == [("payments", "datastore/pay-db"), ("shop", "datastore/shop-db")]
    assert all(s.kind == "datastore" and s.evidence[0]["tool"] == "datastore_clusters" for s in check.signals)

    STATE["connections"] = 10.0
    STATE["down"] = {"pay-db"}
    check = await _evaluate(env_id, _spec())
    assert check.status == "ok" and not check.complete
    assert check.proves_absent("shop") and not check.proves_absent("payments")  # an unread cluster proves nothing

    STATE["down"] = {"pay-db", "shop-db"}
    assert (await _evaluate(env_id, _spec())).status == "unknown"


async def test_detection_respects_rule_namespaces_and_needs_a_cluster():
    env_id, _ = await _env([("shop-db", "shop"), ("pay-db", "payments")])
    check = await _evaluate(env_id, _spec(namespaces=["shop"]))
    assert [s.resource for s in check.signals] == ["datastore/shop-db"]
    bare = await make_env(f"dsd-none-{uuid.uuid4().hex[:6]}")
    assert (await _evaluate(bare, _spec())).status == "unknown"


def test_datastore_rules_validate_and_skills_follow_findings_to_the_cluster():
    with pytest.raises(ValueError):
        skills.DetectionSpec(id="x-rule", kind="datastore")
    lib = skills.load("")
    pg, redis = lib.skills["postgres-health"], lib.skills["redis-health"]
    assert {d.kind for d in pg.meta.detection} == {"datastore"} and {d.finding for d in redis.meta.detection} <= set(datastores.FINDING_KINDS)
    assert skills.availability(pg, {"postgres"}, None)["available"] and not skills.availability(redis, {"postgres"}, None)["available"]
    from moks.skills.validate import errors, validate

    assert errors(validate(pg, lib.skills)) == [] and errors(validate(redis, lib.skills)) == []
    data = [{"name": "shop-db", "kind": "postgres", "namespace": "shop", "findings": [{"severity": "bad", "code": "pg_blocked:shop-db", "text": "6 waiting"},
                                                                                    {"severity": "warn", "code": "not_a_kind:x", "text": "ignored"}]}]
    found = datastores.as_investigation_findings("datastore_clusters", data, {})
    assert found == [{**found[0], "kind": "pg_blocked", "severity": "high", "resources": ["postgres/shop-db"], "namespace": "shop"}] and len(found) == 1
    step = next(s for s in pg.meta.steps if s.tool == "pg_locks")
    assert skills.contexts_for(step, found, {})[0]["cluster"] == "shop-db"
    assert skills.render(step.args, skills.contexts_for(step, found, {})[0]) == {"cluster": "shop-db"}


def test_crew_binds_cluster_tools_without_widening_scope():
    target = Target("mk4", "shop", "checkout", "Deployment", 30, "")
    assert bind(BY_NAME["database_locks"], target, {"cluster": "shop-db"}, log_lines=10) == {"cluster": "shop-db"}
    assert bind(BY_NAME["datastore_health"], target, {}, log_lines=10) == {"health": True}
    with pytest.raises(Exception):
        bind(BY_NAME["database_locks"], target, {"cluster": "../x"}, log_lines=10)
    assert {"datastore_clusters", "pg_locks", "pg_sessions", "redis_overview"} <= ALLOWED_MOKS_TOOLS <= set(REGISTRY)


async def test_data_page_api_reads_through_the_executor(client):
    env_id, name = await _env([("shop-db", "shop")])
    h = await login(client, "admin@example.com")
    r = await client.get(f"/moks/api/datastores/{name}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["data"][0]["name"] == "shop-db" and r.json()["data"][0]["findings"][0]["code"] == "pg_connections:shop-db"
    r = await client.get(f"/moks/api/datastores/{name}/postgres/shop-db/locks", headers=h)
    assert r.status_code == 200 and r.json()["data"]["root_blockers"] == []
    assert (await client.get(f"/moks/api/datastores/{name}/postgres/shop-db/slowlog", headers=h)).status_code == 404
    assert (await client.get(f"/moks/api/datastores/{name}/postgres/nope/overview", headers=h)).status_code == 404
    assert (await client.get(f"/moks/api/datastores/{name}/mysql/x/overview", headers=h)).status_code == 422
