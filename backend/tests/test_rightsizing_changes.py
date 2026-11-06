"""Rightsizing: what changes, per field and per resource, so the table, the CSV and a proposal can never disagree.

The kindnet-cni case is mk4's real DaemonSet: requests and limits of CPU 100m and memory 50Mi on 5 nodes, with
working-set memory near the limit and almost no CPU. It used to read as one "increase" with "cpu 100m→10m,
mem 64Mi→64Mi": the current request was rounded up to 64Mi for display, the memory limit (the field that
actually had to grow) was left out, and a CPU decrease was labelled an increase.
"""
from __future__ import annotations

import pytest

from moks.actions.catalog import PatchResourcesP
from moks.connectors.finops import rightsize
from moks.tools import phase2

Mi = 1024**2
KINDNET = {"namespace": "kube-system", "workload": "DaemonSet/kindnet", "container": "kindnet-cni", "replicas": 5,
           "cpu_request": 0.1, "cpu_limit": 0.1, "mem_request": 50 * Mi, "mem_limit": 50 * Mi, "cpu_p95": 0.0012, "mem_max": 47 * Mi, "coverage": 1.0}
PRICES = (0.0041, 0.0005)  # per vCPU-hour and GiB-hour, roughly mk4's fixed price spread over its capacity


def by_resource(row):
    return {r["resource"]: r for r in row["recommendations"]}


def fields(rec):
    return {c["field"]: c for c in rec["changes"]}


def test_kindnet_memory_and_cpu_are_separate_recommendations_with_true_values():
    [row] = rightsize([KINDNET], *PRICES)
    assert row["current"] == {"cpu_request": "100m", "cpu_limit": "100m", "mem_request": "50Mi", "mem_limit": "50Mi"}  # exact, not rounded up
    mem, cpu = by_resource(row)["memory"], by_resource(row)["cpu"]

    assert mem["action"] == "increase"
    m = fields(mem)
    assert set(m) == {"mem_request", "mem_limit"}
    assert (m["mem_request"]["current"], m["mem_request"]["recommended"], m["mem_request"]["direction"]) == ("50Mi", "64Mi", "increase")
    assert (m["mem_limit"]["current"], m["mem_limit"]["recommended"], m["mem_limit"]["direction"]) == ("50Mi", "96Mi", "increase")
    assert "OOM" in m["mem_limit"]["reason"] and m["mem_request"]["reason"]
    assert mem["patch"] == {"requests": {"memory": "64Mi"}, "limits": {"memory": "96Mi"}}

    assert cpu["action"] == "reduce"
    c = fields(cpu)
    assert set(c) == {"cpu_request"}  # the limit is not changing, so it is not in the recommendation
    assert (c["cpu_request"]["current"], c["cpu_request"]["recommended"], c["cpu_request"]["direction"]) == ("100m", "10m", "decrease")
    assert cpu["patch"] == {"requests": {"cpu": "10m"}, "limits": {}}
    assert cpu["capacity"]["cpu_cores"] == pytest.approx(0.45) and mem["capacity"]["memory_bytes"] == -14 * Mi * 5

    assert row["recommended"] == {"cpu_request": "10m", "cpu_limit": "100m", "mem_request": "64Mi", "mem_limit": "96Mi"}


def test_no_change_ever_shows_equal_values_or_contradicts_its_action():
    rows = rightsize([KINDNET,
                      {**KINDNET, "workload": "Deployment/big", "container": "app", "cpu_request": 2.0, "cpu_limit": 0, "mem_request": 4096 * Mi, "mem_limit": 0, "cpu_p95": 0.1, "mem_max": 300 * Mi},
                      {**KINDNET, "workload": "Deployment/hot", "container": "app", "cpu_request": 0.1, "cpu_limit": 0.2, "cpu_p95": 0.3, "mem_max": 20 * Mi}], *PRICES)
    for row in rows:
        for rec in row["recommendations"]:
            assert rec["changes"], rec
            for ch in rec["changes"]:
                assert ch["current"] != ch["recommended"], ch
                assert ch["reason"]
                assert rec["action"] != "reduce" or ch["direction"] == "decrease"
                assert rec["action"] != "increase" or ch["direction"] in ("increase", "set")
                assert row["recommended"][ch["field"]] == ch["recommended"]  # the merged view agrees with every change
            PatchResourcesP.model_validate({"container": row["container"], **rec["patch"]})
    hot = by_resource(next(r for r in rows if r["workload"] == "Deployment/hot"))["cpu"]
    assert fields(hot)["cpu_request"]["recommended"] == "380m" and fields(hot)["cpu_limit"]["recommended"] == "380m"  # a request above its limit is invalid


def test_only_a_missing_memory_request_recommends_only_that():
    [row] = rightsize([{**KINDNET, "workload": "Deployment/w", "container": "app", "cpu_request": 0.05, "cpu_limit": 0, "mem_request": 0, "mem_limit": 256 * Mi,
                        "cpu_p95": 0.045, "mem_max": 40 * Mi}], *PRICES)
    [mem] = row["recommendations"]
    assert mem["resource"] == "memory" and mem["action"] == "set_requests"
    assert list(fields(mem)) == ["mem_request"] and fields(mem)["mem_request"]["current"] is None and fields(mem)["mem_request"]["direction"] == "set"
    assert mem["patch"] == {"requests": {"memory": "64Mi"}, "limits": {}}


async def test_the_tool_reports_how_prices_were_set(monkeypatch):
    def pod(name):
        return {"metadata": {"name": name, "namespace": "kube-system", "ownerReferences": [{"kind": "DaemonSet", "name": "kindnet"}]}, "status": {"phase": "Running"},
                "spec": {"containers": [{"name": "kindnet-cni", "resources": {"requests": {"cpu": "100m", "memory": "50Mi"}, "limits": {"cpu": "100m", "memory": "50Mi"}}}]}}
    pod_names = [f"kindnet-{i}abcd" for i in range(5)]  # one per mk4 node
    keys = [("kube-system", n, "kindnet-cni") for n in pod_names]

    async def namespaces(ctx, ns):
        return ["kube-system"]

    async def pods(ctx, names, ns):
        return {"kube-system": [pod(n) for n in pod_names]}, {}

    async def usage(ctx, names, days):
        return {"cpu_p95": dict.fromkeys(keys, 0.0012), "mem_max": dict.fromkeys(keys, 47 * Mi), "samples": dict.fromkeys(keys, 672)}, {}, []

    async def prices(ctx):
        return *PRICES, "fixed 60 USD/month split 50% CPU / 50% memory by capacity", 1.0

    for name, fn in (("_namespaces", namespaces), ("_pods_by_namespace", pods), ("_usage", usage), ("_prices", prices)):
        monkeypatch.setattr(phase2, name, fn)
    r = await phase2.finops_rightsizing(None, phase2.AllocArgs(namespace="kube-system", days=7))
    assert r.data["pricing"]["kind"] == "fixed"
    [row] = r.data["recommendations"]
    assert row["current"]["mem_request"] == "50Mi" and {x["resource"] for x in row["recommendations"]} == {"memory", "cpu"}


def write_frontend_fixture(path: str = "../frontend/src/fixtures/rightsizing.json") -> None:
    """The engine's output the frontend tests read, so the UI, CSV and proposal are tested on what the API sends."""
    import json

    from moks.tools.phase2 import _pricing

    checkout = {**KINDNET, "workload": "Deployment/checkout", "namespace": "shop", "container": "app", "cpu_request": 0.1, "cpu_limit": 0, "cpu_p95": 0.18,
                "mem_request": 128 * Mi, "mem_limit": 256 * Mi, "mem_max": 120 * Mi}
    rows = [{**r, "environment": "mk4"} for r in rightsize([KINDNET, checkout], *PRICES)]
    with open(path, "w") as f:
        json.dump({"_source": "backend/moks/connectors/finops.py rightsize() on tests/test_rightsizing_changes.py KINDNET; see write_frontend_fixture()",
                   "rows": rows, "pricing": _pricing("fixed 60 USD/month split 50% CPU / 50% memory by capacity")}, f, indent=1)


async def test_the_api_returns_the_engine_values_for_kindnet(client, monkeypatch):
    """Engine → executor → /finops/overview, with the portal fixture's cluster: the API passes the values on unchanged."""
    import uuid

    from moks.connectors import registry
    from tests.conftest import login, make_env
    from tests.portal_fixtures import FIXTURE_TYPES

    for k, v in FIXTURE_TYPES.items():
        monkeypatch.setitem(registry.CONNECTOR_TYPES, k, v)
    name = f"rs-{uuid.uuid4().hex[:6]}"
    await make_env(name, connectors=[{"type": "kubernetes", "name": "k", "config": {}}, {"type": "prometheus", "name": "p", "config": {"base_url": "http://x.invalid"}},
                                     {"type": "cost_model", "name": "c", "config": {"monthly_cost_usd": 60}}])
    await login(client, "admin@example.com")
    r = await client.get("/moks/api/finops/overview", params={"environment": name, "namespace": "kube-system", "days": 7})
    assert r.status_code == 200, r.text
    [env] = r.json()["environments"]
    assert env["rightsizing_pricing"]["kind"] == "fixed"
    [row] = [x for x in env["rightsizing"] if x["container"] == "kindnet-cni"]
    assert row["current"] == {"cpu_request": "100m", "cpu_limit": "100m", "mem_request": "50Mi", "mem_limit": "50Mi"}
    mem = by_resource(row)["memory"]
    assert mem["action"] == "increase" and mem["patch"] == {"requests": {"memory": "64Mi"}, "limits": {"memory": "96Mi"}}
