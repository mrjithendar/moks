"""Live integration checks against a real cluster. Skipped unless MOKS_LIVE=1.

  MOKS_LIVE=1
  MOKS_CREDENTIALS_DIR=<dir containing a kubeconfig file named 'kubeconfig'>
  MOKS_LIVE_CONTEXT=mk4
  MOKS_LIVE_PROMETHEUS=http://localhost:19090/prometheus   (kubectl port-forward svc/prometheus)
  MOKS_LIVE_LOKI=http://localhost:13100                     (kubectl port-forward svc/loki)
  MOKS_LIVE_NAMESPACE=roboshop-ai

Everything here is read-only.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("MOKS_LIVE") != "1", reason="live cluster checks need MOKS_LIVE=1")

NS = os.environ.get("MOKS_LIVE_NAMESPACE", "roboshop-ai")
NOW = datetime.now(timezone.utc)


def k8s():
    import moks.connectors.base as base
    from pathlib import Path

    base.CREDENTIALS_DIR = Path(os.environ["MOKS_CREDENTIALS_DIR"])
    from moks.connectors.kubernetes import KubernetesConnector

    return KubernetesConnector({"mode": "kubeconfig", "context": os.environ.get("MOKS_LIVE_CONTEXT")}, {"type": "file", "path": "kubeconfig"})


async def test_live_kubernetes_reads():
    k = k8s()
    try:
        msg = await k.test()
        print("\n" + msg)
        nss = await k.list_namespaces()
        assert NS in nss
        pods, _ = await k.list_pods(NS)
        assert pods, "expected pods in the live namespace"
        services = await k.services(NS)
        workloads = await k.workloads(NS)
        events = await k.events(NS, warnings_only=False, limit=20)
        nodes = await k.nodes()
        print(f"{len(pods)} pods, {len(services)} services, {len(workloads)} workloads, {len(events)} events, {len(nodes)} nodes")
        logs = await k.pod_logs(NS, pods[0]["name"], None, False, 5, None)
        assert isinstance(logs, str)
        dep = next(w for w in workloads if w["kind"] == "Deployment")
        hist = await k.rollout_history(NS, dep["name"])
        assert hist["revisions"]
    finally:
        await k.aclose()


async def test_live_prometheus():
    from moks.connectors.prometheus import PrometheusConnector

    p = PrometheusConnector({"base_url": os.environ["MOKS_LIVE_PROMETHEUS"]})
    try:
        print("\n" + await p.test())
        r = await p.instant("up", NOW)
        assert r["series"]
        rng = await p.range(f'sum by (pod) (container_memory_working_set_bytes{{namespace="{NS}", container!=""}})', NOW - timedelta(minutes=30), NOW, 60)
        print(f"up: {len(r['series'])} series; memory: {len(rng['series'])} series")
        alerts = await p.alerts()
        print(f"{len(alerts)} alert rules")
    finally:
        await p.aclose()


async def test_live_loki():
    from moks.connectors.loki import LokiConnector

    lk = LokiConnector({"base_url": os.environ["MOKS_LIVE_LOKI"]})
    try:
        print("\n" + await lk.test())
        r = await lk.query_range(f'{{namespace="{NS}"}}', NOW - timedelta(minutes=60), NOW, limit=5)
        print(f"{len(r['lines'])} log lines")
        assert r["resultType"] == "streams"
    finally:
        await lk.aclose()


async def test_live_end_to_end_investigation():
    """Full deterministic investigation through the policy-checked executor."""
    import moks.connectors.base as base
    from pathlib import Path

    from moks.db import session_scope
    from moks.models import Investigation, User
    from moks.orchestrator.engine import Investigator
    from sqlalchemy import select
    from tests.conftest import make_env, make_user

    base.CREDENTIALS_DIR = Path(os.environ["MOKS_CREDENTIALS_DIR"])
    name = f"live-{uuid.uuid4().hex[:6]}"
    await make_env(name, connectors=[
        {"type": "kubernetes", "name": "k8s", "config": {"mode": "kubeconfig", "context": os.environ.get("MOKS_LIVE_CONTEXT")},
         "credential_ref": {"type": "file", "path": "kubeconfig"}},
        {"type": "prometheus", "name": "prom", "config": {"base_url": os.environ["MOKS_LIVE_PROMETHEUS"]}},
        {"type": "loki", "name": "loki", "config": {"base_url": os.environ["MOKS_LIVE_LOKI"]}},
    ])
    await make_user("live@example.com", [f"investigator:{name}:{NS}"])
    async with session_scope() as db:
        uid = (await db.execute(select(User.id).where(User.email == "live@example.com"))).scalar_one()
        env_id = (await db.execute(select(__import__("moks.models", fromlist=["Environment"]).Environment.id)
                                   .where(__import__("moks.models", fromlist=["Environment"]).Environment.name == name))).scalar_one()
        inv = Investigation(created_by=uid, environment_id=env_id, namespace=NS, question="Show pods restarting in the last 30 minutes",
                            time_from=NOW - timedelta(minutes=30), time_to=NOW, status="running")
        db.add(inv)
        await db.flush()
        iid = inv.id
    await Investigator(iid, "live-test").run()
    async with session_scope() as db:
        done = await db.get(Investigation, iid)
    print(f"\nstatus={done.status} tool_calls={done.tool_calls} summary={done.report and done.report['summary']}")
    for f in (done.report or {}).get("findings", [])[:8]:
        print(" -", f["severity"], f["title"])
    assert done.status == "completed", done.error
