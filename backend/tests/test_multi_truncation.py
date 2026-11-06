"""A namespace bigger than the executor's result bound is reported as truncated, never shown as all of it."""
from __future__ import annotations

import uuid

from moks.connectors import registry
from tests.conftest import login, make_env
from tests.portal_fixtures import FIXTURE_TYPES, SCENARIO


async def test_a_cut_pod_list_says_how_much_came_back(client, monkeypatch):
    for k, v in FIXTURE_TYPES.items():
        monkeypatch.setitem(registry.CONNECTOR_TYPES, k, v)
    name = f"mt-{uuid.uuid4().hex[:6]}"
    await make_env(name, connectors=[{"type": "kubernetes", "name": "k", "config": {}}])
    await login(client, "admin@example.com")
    big = (await client.get("/moks/api/multi/pods", params={"environment": name, "namespace": "shop"})).json()
    total = len(SCENARIO["shop"]["pods"])
    assert 0 < len(big["items"]) < total
    [cut] = big["truncated"]
    assert cut == {"environment": name, "namespace": "shop", "shown": len(big["items"]), "summary": cut["summary"]} and cut["summary"].startswith(f"{total} pods in shop")
    small = (await client.get("/moks/api/multi/pods", params={"environment": name, "namespace": "kube-system"})).json()
    assert small["truncated"] == [] and len(small["items"]) == len(SCENARIO["kube-system"]["pods"])
