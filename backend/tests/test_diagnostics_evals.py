"""Diagnostic sessions (catalog-only commands, SSRF guard, no approval, quick checks, TTL cleanup) and evaluation runs."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from moks.actions import executor as ex
from moks.db import session_scope
from moks.diagnostics import catalog
from moks.models import ActionPlan, DiagnosticSession
from tests.conftest import login, make_env, make_user
from tests.fakes import FakeWriter


@pytest.fixture(autouse=True)
async def no_leftover_plans():
    yield
    async with session_scope() as db:  # sessions queue themselves, and the next test's executor would claim them
        await db.execute(text("UPDATE action_plans SET status = 'cancelled' WHERE status IN ('queued', 'executing')"))


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
    assert s["plan"]["status"] == "queued"  # no second step
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


@pytest.mark.parametrize("prod,body", [(True, {"image": "netshoot"}), (False, {"image": "python", "packages": ["bind-tools"]}), (True, {"image": "python", "packages": ["bind-tools"]})])
async def test_sessions_need_no_approval_even_in_production_or_elevated(client, prod, body):
    name, op, ap = await _env(prod=prod)
    s = (await client.post("/moks/api/diagnostics/sessions", json={"environment": name, "reason": "checking the network", **body}, headers=await login(client, op))).json()
    assert s["plan"]["requires_approval"] is False and s["plan"]["status"] == "queued"
    w = FakeWriter()

    async def factory(env, connector_id=None):
        return w
    ex.writer_factory = factory
    await ex.execute_plan(await ex.claim_plan("t"))
    async with session_scope() as db:
        assert (await db.get(ActionPlan, uuid.UUID(s["plan"]["id"]))).status == "succeeded"
    audit = await _audited("diagnostic.request")
    assert audit[-1].details["elevated"] is bool(body.get("packages"))


async def _audited(action):
    from sqlalchemy import select

    from moks.models import AuditEvent
    async with session_scope() as db:
        return (await db.execute(select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.id))).scalars().all()


async def test_someone_who_cannot_execute_leaves_the_session_waiting(client):
    name, op, ap = await _env()
    viewer = f"req-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(viewer, [f"operator:{name}:shop"])  # may request here, but not execute in the diagnostics namespace
    s = (await client.post("/moks/api/diagnostics/sessions", json={"environment": name, "image": "netshoot", "reason": "from a narrow binding"}, headers=await login(client, viewer))).json()
    assert s["plan"]["status"] == "pending"


async def test_a_pod_that_cannot_be_created_fails_the_session_with_the_reason(client):
    from moks.connectors.base import ConnectorError
    from moks.diagnostics import service as diag

    class Refused(FakeWriter):
        async def create_pod(self, ns, manifest):
            raise ConnectorError("forbidden", "quota exceeded")
    name, op, _ = await _env()
    s = (await client.post("/moks/api/diagnostics/sessions", json={"environment": name, "image": "netshoot", "reason": "quota check"}, headers=await login(client, op))).json()
    async with session_scope() as db:
        plan = await db.get(ActionPlan, uuid.UUID(s["plan"]["id"]))
    out = await diag.start(plan, Refused())
    assert not out["ok"]
    async with session_scope() as db:
        row = await db.get(DiagnosticSession, uuid.UUID(s["id"]))
        assert row.status == "failed" and "quota exceeded" in row.error


async def test_quick_check_queues_ordinary_audited_commands(client):
    name, op, _ = await _env()
    h = await login(client, op)
    s = (await client.post("/moks/api/diagnostics/sessions", json={"environment": name, "image": "netshoot", "reason": "reach checkout"}, headers=h)).json()
    async with session_scope() as db:
        (await db.get(DiagnosticSession, uuid.UUID(s["id"]))).status = "ready"
    out = (await client.post(f"/moks/api/diagnostics/sessions/{s['id']}/check", json={"target": "https://checkout.shop/health"}, headers=h)).json()
    assert [c["tool"] for c in out["commands"]] == ["dns_lookup", "tcp_check", "tls_expiry", "http_check"]
    assert all(c["status"] == "queued" for c in out["commands"])
    assert (await client.post(f"/moks/api/diagnostics/sessions/{s['id']}/check", json={"target": "http://169.254.169.254/"}, headers=h)).status_code == 422


def test_quick_check_fits_the_target_and_the_image():
    tools = lambda image, target: [t for t, _ in catalog.quick_check(image, target)]  # noqa: E731
    assert tools("netshoot", "db.shop:5432") == ["dns_lookup", "tcp_check", "http_check"]
    assert tools("netshoot", "10.0.0.1:443") == ["tcp_check", "tls_expiry", "http_check"]
    assert tools("curl", "https://a.example/") == ["http_check"] and tools("python", "https://a.example/") == ["http_check_py"]


@pytest.mark.parametrize("tool,code,output,expected", [
    ("tcp_check", 1, "connect: Connection refused", "nothing listens"), ("tcp_check", 1, "Operation timed out", "NetworkPolicy"),
    ("tcp_check", 0, "open", "accepts"), ("dns_lookup", 0, "", "does not resolve"), ("http_check", 0, "status=503 remote=1.1.1.1:80", "failing"),
    ("http_check", 0, "status=403 remote=1.1.1.1:80", "refused this request"), ("http_check", 0, "status=200 remote=1.1.1.1:80", "answered 200"),
])
def test_results_are_explained(tool, code, output, expected):
    assert expected in catalog.interpret(tool, code, output)


def test_nothing_is_claimed_without_evidence():
    assert catalog.interpret("tcp_check", 2, "odd failure") is None and catalog.interpret("http_get", 0, "x") is None
    assert catalog.interpret("tcp_check", None, "") is None


async def test_lab_evaluation_scores_rules_mode():
    from moks.evals import runner

    rid = await runner.start("lab", "rules")
    out = await runner.execute(rid)
    assert out["cases"] == 6 and out["score"] == 1.0


async def test_a_session_whose_plan_expired_does_not_stay_requested(client):
    from sqlalchemy import update as sql_update

    from moks.db import session_scope
    from moks.models import ActionPlan, DiagnosticSession

    name, op, _ = await _env()
    h = await login(client, op)
    s = (await client.post("/moks/api/diagnostics/sessions", json={"environment": name, "image": "python", "ttl_minutes": 15, "reason": "stuck session check"}, headers=h)).json()
    async with session_scope() as db:
        await db.execute(sql_update(ActionPlan).where(ActionPlan.id == uuid.UUID(s["plan"]["id"])).values(status="expired"))
        await ex.close_undecided_sessions(db)
    async with session_scope() as db:
        assert (await db.get(DiagnosticSession, uuid.UUID(s["id"]))).status == "expired"
