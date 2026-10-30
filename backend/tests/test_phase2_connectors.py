"""Phase 2 connectors and FinOps: Grafana, Git (no guessing), CloudWatch, EKS token, Cost Explorer, allocation math."""
import asyncio
import base64
from datetime import datetime, timedelta, timezone

import boto3
import httpx
import pytest
from botocore.stub import Stubber

from moks.connectors.base import ConnectorError
from moks.connectors.finops import AwsCostConnector, CostModelConnector, allocate, parse_cpu, parse_mem, rightsize
from moks.connectors.git import GitConnector
from moks.connectors.grafana import GrafanaConnector

NOW = datetime.now(timezone.utc)
SESSION = boto3.session.Session(aws_access_key_id="AKIAEXAMPLEEXAMPLE00", aws_secret_access_key="x" * 40, region_name="us-east-1")


async def test_grafana_dashboards_panels_alerts():
    def h(req: httpx.Request):
        p = req.url.path
        if p == "/grafana/api/search":
            return httpx.Response(200, json=[{"uid": "abc", "title": "Checkout", "url": "/grafana/d/abc/checkout", "tags": ["shop"]}])
        if p == "/grafana/api/dashboards/uid/abc":
            return httpx.Response(200, json={"meta": {"url": "/grafana/d/abc/checkout"}, "dashboard": {"title": "Checkout", "panels": [
                {"type": "row", "panels": [{"id": 2, "title": "5xx", "type": "timeseries", "targets": [{"expr": "sum(rate(http_5xx[5m]))"}]}]}]}})
        if p == "/grafana/api/prometheus/grafana/api/v1/rules":
            return httpx.Response(200, json={"data": {"groups": [{"name": "g", "rules": [{"type": "alerting", "name": "High5xx", "state": "firing",
                                                                                         "alerts": [{"labels": {"namespace": "shop"}, "state": "Alerting"}]}]}]}})
        return httpx.Response(404)

    g = GrafanaConnector({"base_url": "http://grafana/grafana", "public_url": "https://jithendar.com/grafana"}, transport=httpx.MockTransport(h))
    rows = await g.search_dashboards("checkout", None, 5, NOW - timedelta(hours=1), NOW)
    assert rows[0]["url"].startswith("https://jithendar.com/grafana/d/abc/checkout?from=")
    d = await g.dashboard("abc")
    assert d["panels"][0]["queries"] == ["sum(rate(http_5xx[5m]))"] and "viewPanel=2" in d["panels"][0]["url"]
    assert (await g.alert_rules())[0]["active"][0]["labels"]["namespace"] == "shop"


def _github(commit_known=True):
    def h(req: httpx.Request):
        p = req.url.path
        if p == "/repos/acme/shop":
            return httpx.Response(200, json={"default_branch": "main"})
        if p.startswith("/repos/acme/shop/commits/") and commit_known:
            return httpx.Response(200, json={"sha": "ab12cd34" + "0" * 32, "commit": {"message": "fix checkout", "author": {"name": "dev", "date": "2026-10-30"}},
                                             "html_url": "https://github.com/acme/shop/commit/ab12cd34", "files": []})
        if p.startswith("/repos/acme/shop/compare/"):
            return httpx.Response(200, json={"status": "behind", "ahead_by": 3, "behind_by": 0, "commits": [], "files": []})
        return httpx.Response(404, json={"message": "Not Found"})
    return httpx.MockTransport(h)


async def test_git_never_guesses_the_deployed_revision():
    git = GitConnector({"repositories": ["acme/shop"]}, transport=_github())
    none = await git.compare_deployment("acme/shop", ["registry/shop:latest"], {})
    assert none["resolved"] is False and "cannot be determined" in none["reason"]
    yes = await git.compare_deployment("acme/shop", ["registry/shop:ab12cd34"], {})
    assert yes["resolved"] and yes["source"] == "image tag" and yes["behind_default_branch_by"] == 3
    unknown = await GitConnector({"repositories": ["acme/shop"]}, transport=_github(commit_known=False)).compare_deployment(
        "acme/shop", ["registry/shop:deadbeef"], {})
    assert unknown["resolved"] is False and "is not a commit" in unknown["reason"]
    with pytest.raises(ConnectorError):
        await git.read_file("acme/other", "README.md", None)  # outside the allowlist
    with pytest.raises(ConnectorError):
        await git.provider.read("acme/shop", "deploy/.env", None)  # secret paths are never read


def test_eks_token_is_a_presigned_sts_call():
    from urllib.parse import parse_qs, urlsplit

    from moks.connectors.kubernetes import eks_token

    t = eks_token(SESSION, "prod", "us-east-1")
    url = base64.urlsafe_b64decode(t.removeprefix("k8s-aws-v1.") + "==").decode()
    q = parse_qs(urlsplit(url).query)
    assert t.startswith("k8s-aws-v1.") and q["Action"] == ["GetCallerIdentity"] and "x-k8s-aws-id" in q["X-Amz-SignedHeaders"][0]


async def test_cost_explorer_is_cached_and_capped():
    c = AwsCostConnector({"cache_minutes": 60, "max_requests_per_hour": 2}, session=SESSION)
    ce = SESSION.client("ce", region_name="us-east-1")
    stub = Stubber(ce)
    day = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    resp = {"ResultsByTime": [{"TimePeriod": {"Start": day, "End": day}, "Groups": [
        {"Keys": ["Amazon EC2"], "Metrics": {"UnblendedCost": {"Amount": "12.5", "Unit": "USD"}}},
        {"Keys": ["Amazon S3"], "Metrics": {"UnblendedCost": {"Amount": "1.25", "Unit": "USD"}}}], "Estimated": False}]}
    stub.add_response("get_cost_and_usage", resp)
    stub.add_response("get_cost_and_usage", resp)
    stub.activate()
    c._client = lambda name: ce
    r1 = await c.costs("service", 7)
    r2 = await c.costs("service", 7)  # cached: no second request
    assert r1 == r2 and r1["total"] == 13.75 and r1["groups"][0]["name"] == "Amazon EC2"
    await c.costs("service", 8)
    with pytest.raises(ConnectorError) as e:
        await c.costs("service", 9)  # third distinct request exceeds the cap of 2/hour
    assert "cap" in e.value.message
    with pytest.raises(ConnectorError):
        await c.costs("tag", 7, tag_key="team")  # tags must be configured allocation tags


def test_cost_model_and_allocation_math():
    assert parse_cpu("250m") == 0.25 and parse_mem("512Mi") == 512 * 1024**2 and parse_mem("1Gi") == 1024**3
    model = CostModelConnector({"monthly_cost_usd": 73.0, "cpu_share": 0.5})
    cpu_p, gib_p = model.unit_prices(total_cpu=10, total_gib=20)
    assert round(cpu_p, 6) == round(0.1 * 0.5 / 10, 6) and round(gib_p, 6) == round(0.1 * 0.5 / 20, 6)
    rows = allocate([{"namespace": "shop", "workload": "Deployment/a", "cpu_request": 1.0, "mem_request": 1024**3, "cpu_avg": 0.25, "mem_avg": 512 * 1024**2}],
                    cpu_p, gib_p, hours=24)
    r = rows[0]
    assert r["cpu_efficiency"] == 0.25 and r["idle_cost"] > 0 and r["cost"] > r["idle_cost"]
    recs = rightsize([
        {"namespace": "shop", "workload": "Deployment/big", "container": "app", "replicas": 3, "cpu_request": 2.0, "cpu_limit": 0, "mem_request": 4 * 1024**3,
         "mem_limit": 4 * 1024**3, "cpu_p95": 0.1, "mem_max": 300 * 1024**2},
        {"namespace": "shop", "workload": "Deployment/tight", "container": "app", "replicas": 1, "cpu_request": 0.1, "cpu_limit": 0, "mem_request": 64 * 1024**2,
         "mem_limit": 64 * 1024**2, "cpu_p95": 0.3, "mem_max": 62 * 1024**2},
    ], 0.04, 0.005)
    by = {x["workload"]: x for x in recs}
    assert by["Deployment/tight"]["action"] == "increase" and any("OOM" in f for f in by["Deployment/tight"]["findings"])
    assert by["Deployment/big"]["action"] == "reduce" and by["Deployment/big"]["monthly_saving"] > 0
    assert recs[0]["action"] == "increase"  # risks first


async def test_finops_tools_need_finops_permission(client):
    import uuid

    from tests.conftest import login, make_env, make_user

    name = f"fin-{uuid.uuid4().hex[:6]}"
    await make_env(name, connectors=[{"type": "cost_model", "name": "cm", "config": {"monthly_cost_usd": 50}}])
    await make_user(f"v-{name}@example.com", [f"viewer:{name}:*"])
    await login(client, f"v-{name}@example.com")
    r = await client.get(f"/moks/api/finops/{name}/k8s/allocation", params={"namespace": "shop"})
    assert r.status_code == 403
