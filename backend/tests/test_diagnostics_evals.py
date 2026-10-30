"""Diagnostic sessions (catalog-only commands, SSRF guard, approval for elevated, TTL cleanup) and evaluation runs."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from moks.actions import executor as ex
from moks.db import session_scope
from moks.diagnostics import catalog
from moks.models import DiagnosticSession
from tests.conftest import login, make_env, make_user
from tests.fakes import FakeWriter


def test_command_templates_validate_arguments():
    assert catalog.build_argv("netshoot", "dns_lookup", {"name": "checkout.shop.svc.cluster.local", "type": "srv"})[-1] == "SRV"
    for bad in ({"url": "http://169.254.169.254/latest/meta-data"}, {"url": "http://localhost:8080/"}, {"url": "file:///etc/passwd"},
                {"url": "http://x/;rm -rf /"}):
        with pytest.raises(catalog.ArgError):
            catalog.build_argv("netshoot", "http_check", bad)
    with pytest.raises(catalog.ArgError):
        catalog.build_argv("netshoot", "shell", {"cmd": "id"})        # no free-form commands
    with pytest.raises(catalog.ArgError):
        catalog.build_argv("curl", "dns_lookup", {"name": "a"})       # tool not in that image
    with pytest.raises(catalog.ArgError):
        catalog.build_argv("netshoot", "jq_fetch", {"url": "http://svc/", "path": ".a | input"})
    with pytest.raises(catalog.ArgError):
        catalog.check_packages(["netcat-openbsd"], "python")          # not in the catalog
    assert catalog.check_packages(["httpie"], "python") is False and catalog.check_packages(["bind-tools"], "python") is True


async def _env(prod=False):
    name = f"dg-{uuid.uuid4().hex[:6]}"
    env_id = await make_env(name)
    async with session_scope() as db:
        from moks.models import Environment

        (await db.get(Environment, env_id)).is_production = prod
    tag = uuid.uuid4().hex[:6]
    await make_user(f"dop-{tag}@example.com", [f"operator:{name}:*"])
    await make_user(f"dap-{tag}@example.com", [f"approver:{name}:*"])
    return name, f"dop-{tag}@example.com", f"dap-{tag}@example.com"


async def test_session_lifecycle_with_cleanup(client, monkeypatch):
    name, op, ap = await _env()
    h = await login(client, op)
    s = (await client.post("/moks/api/diagnostics/sessions", json={"environment": name, "image": "python", "packages": ["httpie"],
                                                                  "ttl_minutes": 15, "reason": "DNS flapping for checkout"}, headers=h)).json()
    assert s["status"] == "requested" and s["plan"]["requires_approval"] is False and not s["elevated"]
    q = await client.post(f"/moks/api/actions/plans/{s['plan']['id']}/execute", json={"plan_hash": s["plan"]["plan_hash"]}, headers=h)
    assert q.json()["status"] == "queued"
    w = FakeWriter()

    async def factory(env, connector_id=None):
        return w

    monkeypatch.setattr(ex, "writer_factory", factory)
    await ex.execute_plan(await ex.claim_plan("t"))
    pod = next(c[2] for c in w.calls if c[0] == "create_pod")
    spec = pod["spec"]
    assert spec["automountServiceAccountToken"] is False and spec["securityContext"]["runAsNonRoot"] and spec["activeDeadlineSeconds"] == 15 * 60 + 60
    assert any(c[0] == "exec" and c[3][:3] == ["pip", "install", "--user"] for c in w.calls)  # catalog package installed in the pod
    s = (await client.get(f"/moks/api/diagnostics/sessions/{s['id']}")).json()
    assert s["status"] == "ready"
    h = await login(client, op)
    r = await client.post(f"/moks/api/diagnostics/sessions/{s['id']}/commands", json={"tool": "http_check_py", "args": {"url": "http://checkout.shop/"}}, headers=h)
    assert r.status_code == 201
    from moks.diagnostics import service as diag

    await diag.tick(writer_override=w)
    s = (await client.get(f"/moks/api/diagnostics/sessions/{s['id']}")).json()
    assert s["commands"][-1]["status"] == "done" and "status=200" in s["commands"][-1]["output"]
    async with session_scope() as db:  # TTL passes
        (await db.get(DiagnosticSession, uuid.UUID(s["id"]))).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await diag.tick(writer_override=w)
    assert any(c[0] == "delete_pod" for c in w.calls)
    assert (await client.get(f"/moks/api/diagnostics/sessions/{s['id']}")).json()["status"] == "expired"


async def test_elevated_or_production_sessions_need_approval(client):
    name, op, ap = await _env(prod=False)
    h = await login(client, op)
    s = (await client.post("/moks/api/diagnostics/sessions", json={"environment": name, "image": "python", "packages": ["bind-tools"],
                                                                  "reason": "need dig as root"}, headers=h)).json()
    assert s["elevated"] and s["plan"]["requires_approval"]
    r = await client.post(f"/moks/api/actions/plans/{s['plan']['id']}/execute", json={"plan_hash": s["plan"]["plan_hash"]}, headers=h)
    assert r.status_code == 409
    prod, op2, _ = await _env(prod=True)
    h2 = await login(client, op2)
    s2 = (await client.post("/moks/api/diagnostics/sessions", json={"environment": prod, "image": "netshoot", "reason": "prod network check"}, headers=h2)).json()
    assert s2["plan"]["requires_approval"] and not s2["elevated"]


async def test_lab_evaluation_scores_rules_mode():
    from moks.evals import runner

    rid = await runner.start("lab", "rules")
    out = await runner.execute(rid)
    assert out["cases"] == 6 and out["score"] == 1.0
