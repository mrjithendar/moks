"""The terminal's rules: who may open and attach, what ends a session, and what never leaves the gateway."""
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select, update

from moks.config import get_settings
from moks.db import session_scope
from moks.models import AuditEvent, RoleBinding, Session, TerminalSession, User
from moks.rbac import ROLES, Binding, P, Principal
from moks.terminal import api as tapi, aws as taws, pods as tpods, profiles as tprofiles, service
from tests.conftest import login, make_env, make_user

ROLE = "arn:aws:iam::111122223333:role/moks/moks-terminal-readonly"
SECRET = "SECRETKEYVALUE/never-to-be-seen"


class FakeAws:
    def __init__(self, expires_in=3600):
        self.expires_in, self.assumed, self.wrong_identity, self.fail = expires_in, [], None, False

    async def assume(self, profile, region, name, seconds, identity):
        if self.fail:
            raise taws.TerminalError(f"could not assume {profile.role_arn}: AccessDenied")
        self.assumed.append({"role": profile.role_arn, "name": name, "seconds": seconds, "identity": identity, "region": region})
        return taws.Credentials("ASIAFAKE", SECRET, "session-token", datetime.now(timezone.utc) + timedelta(seconds=self.expires_in))

    async def identity(self, creds, region):
        return self.wrong_identity or {"account": "111122223333", "arn": f"arn:aws:sts::111122223333:assumed-role/moks-terminal-readonly/{self.assumed[-1]['name']}"}


class FakeStream:
    def __init__(self):
        self.sent, self.sizes, self.queue, self.closed = [], [], asyncio.Queue(), False

    async def send(self, data):
        self.sent.append(data)

    async def resize(self, cols, rows):
        self.sizes.append((cols, rows))

    async def receive(self):
        return await self.queue.get()

    async def close(self):
        self.closed = True


class FakePods:
    def __init__(self):
        self.manifests, self.files, self.deleted, self.existing, self.commands = [], {}, [], {}, []
        self.stream, self.shell_alive, self.kube_user, self.can_i, self.inside = FakeStream(), True, "system:serviceaccount:moks-terminals:moks-terminal-session", set(), None

    async def aclose(self):
        pass

    async def create(self, manifest):
        self.manifests.append(manifest)
        self.existing[manifest["metadata"]["name"]] = manifest["metadata"]["labels"]["moks.io/session"]

    async def wait_running(self, name, seconds):
        pass

    async def write_file(self, name, path, content):
        self.files[path] = content

    async def server(self):
        return "https://10.96.0.1:443", b"CA"

    async def service_account_token(self, pod, seconds):
        return "bound-token"

    async def run(self, name, argv, stdin=b"", timeout=30):
        self.commands.append(argv)
        if argv[:2] == ["aws", "sts"]:
            arn = self.inside or f"arn:aws:sts::111122223333:assumed-role/moks-terminal-readonly/{name}"
            return 0, json.dumps({"Account": "111122223333", "Arn": arn})
        if argv[:3] == ["kubectl", "auth", "whoami"]:
            return 0, json.dumps({"status": {"userInfo": {"username": self.kube_user}}})
        if argv[:3] == ["kubectl", "auth", "can-i"]:
            return (0 if tuple(argv[3:5]) in self.can_i else 1), ""
        return 0, ""

    async def has_shell(self, pod):
        return self.shell_alive

    async def attach(self, pod):
        return self.stream

    async def sessions(self):
        return dict(self.existing)

    async def delete(self, name):
        self.deleted.append(name)
        self.existing.pop(name, None)


@pytest.fixture
def world(monkeypatch):
    env = f"term-{uuid.uuid4().hex[:6]}"
    cfg = get_settings()
    rows = [{"name": "ro", "environment": env, "account": "111122223333", "roleArn": ROLE, "regions": ["us-east-1"], "eksClusters": ["prod"], "inCluster": True}]
    monkeypatch.setattr(cfg, "terminal_profiles", json.dumps(rows))
    monkeypatch.setattr(cfg, "terminal_image", "registry/moks-terminal@sha256:" + "a" * 64)
    monkeypatch.setattr(cfg, "terminal_kube_namespaces", "shop,moks-lab")
    fake_aws, fake_pods = FakeAws(), FakePods()
    monkeypatch.setattr(service, "aws_factory", lambda: fake_aws)
    monkeypatch.setattr(service, "pods_factory", lambda: fake_pods)
    monkeypatch.setattr(tapi, "WATCH_SECONDS", 0.02)
    return env, fake_aws, fake_pods


@pytest.fixture(scope="session")
def gateway(database):
    from moks.terminal.app import create_app

    return create_app()


@pytest.fixture
async def gw(gateway, app):
    """The gateway, plus the portal that signs people in: the cookie is shared, as under one host."""
    import httpx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://testserver") as c, \
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as portal:
        c.portal = portal
        yield c


def body(env, **over):
    return {"profile": "ro", "environment": env, "account": "111122223333", "region": "us-east-1", "cluster": "in-cluster",
            "purpose": "Checking the checkout rollout", "minutes": 30, **over}


async def person(gw, env, grants=None):
    email = f"t-{uuid.uuid4().hex[:8]}@example.com"
    await make_user(email, grants if grants is not None else [f"terminal:{env}:*"])
    gw.portal.cookies.clear()
    headers = await login(gw.portal, email)
    gw.cookies.clear()
    gw.cookies.set("moks_session", gw.portal.cookies.get("moks_session"))
    return email, headers


async def settle():
    await asyncio.gather(*tapi._tasks, return_exceptions=True)


async def opened(gw, env, headers):
    r = await gw.post("/moks/api/terminal/sessions", json=body(env), headers=headers)
    assert r.status_code == 201, r.text
    await settle()
    out = (await gw.get(f"/moks/api/terminal/sessions/{r.json()['id']}")).json()
    assert out["status"] == "ready", out
    return uuid.UUID(out["id"])


class Socket:
    """Drives the ASGI WebSocket directly, on the test's own event loop."""

    def __init__(self, app, sid, cookie, origin="http://testserver"):
        self.app, self.inbox, self.outbox = app, asyncio.Queue(), asyncio.Queue()
        headers = [(b"host", b"testserver")]
        if origin:
            headers.append((b"origin", origin.encode()))
        if cookie:
            headers.append((b"cookie", f"moks_session={cookie}".encode()))
        self.scope = {"type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "ws", "path": f"/moks/api/terminal/sessions/{sid}/attach",
                      "raw_path": b"", "query_string": b"", "root_path": "", "headers": headers, "client": ("127.0.0.1", 1), "server": ("testserver", 80), "subprotocols": [], "state": {}}
        self.inbox.put_nowait({"type": "websocket.connect"})
        self.task = asyncio.create_task(app(self.scope, self.receive, self.outbox.put))

    async def receive(self):
        return await self.inbox.get()

    async def first(self):
        return await asyncio.wait_for(self.outbox.get(), 5)

    async def close_code(self):
        while True:
            m = await self.first()
            if m["type"] == "websocket.close":
                return m["code"]

    async def send(self, data=None, text=None):
        await self.inbox.put({"type": "websocket.receive", "bytes": data, "text": text})

    async def leave(self):
        await self.inbox.put({"type": "websocket.disconnect", "code": 1001})
        await asyncio.wait_for(self.task, 5)


def cookie(client):
    return dict(client.cookies).get("moks_session")


async def events(action, sid=None):
    async with session_scope() as db:
        q = select(AuditEvent).where(AuditEvent.action == action)
        rows = (await db.execute(q)).scalars().all()
    return [r for r in rows if sid is None or r.resource_id == str(sid)]


# ---- permission -----------------------------------------------------------------------------

def test_terminal_access_is_separate_from_every_other_role():
    assert P.TERMINAL_USE in ROLES["terminal"]
    assert not [r for r, perms in ROLES.items() if P.TERMINAL_USE in perms and r != "terminal"]
    assert ROLES["terminal"] == {P.TERMINAL_USE, P.OVERVIEW_READ, P.INVESTIGATION_READ}


def test_a_namespace_scoped_grant_does_not_open_a_terminal():
    assert not Principal("u", "e", [Binding("terminal", "prod", "shop")]).can(P.TERMINAL_USE, "prod", None)
    assert Principal("u", "e", [Binding("terminal", "prod", "*")]).can(P.TERMINAL_USE, "prod", None)


async def test_no_permission_no_session(world, gw):
    env, fake_aws, _ = world
    await make_env(env)
    _, h = await person(gw, env, [f"operator:{env}:*"])
    r = await gw.post("/moks/api/terminal/sessions", json=body(env), headers=h)
    assert r.status_code == 403 and not fake_aws.assumed
    assert (await gw.get("/moks/api/terminal/options")).json()["profiles"] == []


async def test_an_admin_cannot_open_a_terminal_without_the_grant(world, gw):
    env, fake_aws, _ = world
    await make_env(env)
    _, h = await person(gw, env, ["admin"])
    assert (await gw.post("/moks/api/terminal/sessions", json=body(env), headers=h)).status_code == 403


async def test_signed_out_requests_are_refused(world, gw):
    env, *_ = world
    gw.cookies.clear()
    assert (await gw.get("/moks/api/terminal/options")).status_code == 401
    assert (await gw.post("/moks/api/terminal/sessions", json=body(env))).status_code == 401


@pytest.mark.parametrize("over", [{"account": "999988887777"}, {"region": "eu-west-1"}, {"cluster": "other"}, {"profile": "nope"}])
async def test_only_a_scope_the_profile_offers(world, gw, over):
    env, fake_aws, _ = world
    await make_env(env)
    _, h = await person(gw, env)
    r = await gw.post("/moks/api/terminal/sessions", json=body(env, **over), headers=h)
    assert r.status_code == 422 and not fake_aws.assumed
    assert (await events("terminal.create"))[-1].decision == "deny"


async def test_sessions_per_person_are_limited(world, gw, monkeypatch):
    env, *_ = world
    await make_env(env)
    monkeypatch.setattr(get_settings(), "terminal_user_sessions", 1)
    _, h = await person(gw, env)
    await opened(gw, env, h)
    assert (await gw.post("/moks/api/terminal/sessions", json=body(env), headers=h)).status_code == 422


# ---- identity -------------------------------------------------------------------------------

async def test_a_session_gets_its_own_short_lived_identity(world, gw):
    env, fake_aws, fake_pods = world
    await make_env(env)
    email, h = await person(gw, env)
    sid = await opened(gw, env, h)
    got = fake_aws.assumed[0]
    assert got["role"] == ROLE and got["seconds"] == 1800 and got["identity"] == email and got["name"] == f"moks-term-{str(sid)[:8]}"
    s = (await gw.get(f"/moks/api/terminal/sessions/{sid}")).json()
    assert s["aws_identity"].endswith(f"assumed-role/moks-terminal-readonly/moks-term-{str(sid)[:8]}")
    assert s["kube_identity"].startswith("system:serviceaccount:moks-terminals:") and s["credential_expires_at"]
    assert b"bound-token" in fake_pods.files[tpods.KUBECONFIG]


@pytest.mark.parametrize("found", [{"account": "999988887777", "arn": "arn:aws:sts::999988887777:assumed-role/x/y"},
                                   {"account": "111122223333", "arn": "arn:aws:iam::111122223333:user/moks-gateway"}])
async def test_a_wrong_identity_never_gets_a_pod(world, gw, found):
    env, fake_aws, fake_pods = world
    await make_env(env)
    fake_aws.wrong_identity = found
    _, h = await person(gw, env)
    r = await gw.post("/moks/api/terminal/sessions", json=body(env), headers=h)
    await settle()
    s = (await gw.get(f"/moks/api/terminal/sessions/{r.json()['id']}")).json()
    assert s["status"] == "failed" and "not the selected role" in s["error"] and not fake_pods.manifests


async def test_a_refused_role_fails_and_nothing_else_is_tried(world, gw):
    env, fake_aws, fake_pods = world
    await make_env(env)
    fake_aws.fail = True
    _, h = await person(gw, env)
    r = await gw.post("/moks/api/terminal/sessions", json=body(env), headers=h)
    await settle()
    s = (await gw.get(f"/moks/api/terminal/sessions/{r.json()['id']}")).json()
    assert s["status"] == "failed" and "AccessDenied" in s["error"] and not fake_pods.manifests


async def test_the_shell_must_see_the_same_identity(world, gw):
    env, _, fake_pods = world
    await make_env(env)
    fake_pods.inside = "arn:aws:sts::111122223333:assumed-role/ec2-node-role/i-0123"
    _, h = await person(gw, env)
    r = await gw.post("/moks/api/terminal/sessions", json=body(env), headers=h)
    await settle()
    assert (await gw.get(f"/moks/api/terminal/sessions/{r.json()['id']}")).json()["status"] == "failed"


@pytest.mark.parametrize("risky", [("create", "pods"), ("get", "secrets"), ("*", "*")])
async def test_a_kubernetes_identity_that_can_write_is_refused(world, gw, risky):
    env, _, fake_pods = world
    await make_env(env)
    fake_pods.can_i.add(risky)
    _, h = await person(gw, env)
    r = await gw.post("/moks/api/terminal/sessions", json=body(env), headers=h)
    await settle()
    s = (await gw.get(f"/moks/api/terminal/sessions/{r.json()['id']}")).json()
    assert s["status"] == "failed" and "not a read-only identity" in s["error"]


def test_credentials_cannot_reach_a_log_line():
    c = taws.Credentials("ASIAFAKE", SECRET, "token", datetime.now(timezone.utc))
    assert SECRET not in repr(c) and "ASIAFAKE" not in f"{c}"


async def test_no_credential_is_stored_or_audited(world, gw):
    env, _, fake_pods = world
    await make_env(env)
    _, h = await person(gw, env)
    sid = await opened(gw, env, h)
    assert SECRET.encode() in fake_pods.files[tpods.AWS_FILE]
    async with session_scope() as db:
        row = await db.get(TerminalSession, sid)
        stored = json.dumps(service.session_out(row)) + str({c.name: getattr(row, c.name) for c in row.__table__.columns})
        logged = (await db.execute(select(AuditEvent).where(AuditEvent.resource_id == str(sid)))).scalars().all()
    assert SECRET not in stored and "session-token" not in stored and "bound-token" not in stored
    assert logged and not [e for e in logged if SECRET in json.dumps(e.details) or "bound-token" in json.dumps(e.details)]
    assert SECRET not in json.dumps(fake_pods.manifests)  # the pod spec is readable by anyone with pods get


# ---- the pod --------------------------------------------------------------------------------

async def test_the_pod_meets_the_isolation_rules(world, gw):
    env, _, fake_pods = world
    await make_env(env)
    _, h = await person(gw, env)
    await opened(gw, env, h)
    spec = fake_pods.manifests[0]["spec"]
    c = spec["containers"][0]
    assert spec["automountServiceAccountToken"] is False and spec["enableServiceLinks"] is False
    assert not spec["hostNetwork"] and not spec["hostPID"] and not spec["hostIPC"]
    assert spec["securityContext"]["runAsNonRoot"] is True and spec["securityContext"]["runAsUser"] > 0
    assert spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert c["securityContext"] == {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "privileged": False, "capabilities": {"drop": ["ALL"]}}
    assert {"cpu", "memory", "ephemeral-storage"} <= set(c["resources"]["limits"])
    assert all("emptyDir" in v for v in spec["volumes"])  # no hostPath, no socket, no kubeconfig
    assert next(v for v in spec["volumes"] if v["name"] == "secrets")["emptyDir"]["medium"] == "Memory"
    assert spec["activeDeadlineSeconds"] <= 1800 + 120
    names = {e["name"]: e["value"] for e in c["env"]}
    assert names["AWS_EC2_METADATA_DISABLED"] == "true" and not [k for k in names if "SECRET" in k or "TOKEN" in k or k == "AWS_ACCESS_KEY_ID"]


# ---- attaching ------------------------------------------------------------------------------

async def test_origin_is_checked_before_anything_else(world, gw, gateway):
    env, *_ = world
    await make_env(env)
    _, h = await person(gw, env)
    sid = await opened(gw, env, h)
    for bad in ("https://evil.example", "http://testserver.evil.example", "https://testserver", None):
        ws = Socket(gateway, sid, cookie(gw), origin=bad)
        assert (await ws.first())["type"] == "websocket.close"  # never accepted
        await ws.task
    assert [e for e in await events("terminal.attach", sid) if e.details.get("reason") == "origin"]


async def test_attach_needs_a_signed_in_owner(world, gw, gateway):
    env, *_ = world
    await make_env(env)
    _, h = await person(gw, env)
    sid = await opened(gw, env, h)
    owner = cookie(gw)
    anonymous = Socket(gateway, sid, None)
    assert await anonymous.close_code() == tapi.UNAUTHENTICATED
    await person(gw, env)  # someone else with terminal.use, signed in on the same client
    other = Socket(gateway, sid, cookie(gw))
    assert await other.close_code() == tapi.NOT_FOUND
    assert owner != cookie(gw)
    assert [e for e in await events("terminal.attach", sid) if e.decision == "deny" and e.details.get("reason") == "session not found"]


async def test_an_admin_cannot_attach_to_someone_elses_shell(world, gw, gateway):
    env, *_ = world
    await make_env(env)
    _, h = await person(gw, env)
    sid = await opened(gw, env, h)
    await person(gw, env, ["admin"])
    assert (await gw.get(f"/moks/api/terminal/sessions/{sid}")).status_code == 200  # may see it
    ws = Socket(gateway, sid, cookie(gw))
    assert await ws.close_code() == tapi.NOT_FOUND


async def test_an_attached_terminal_passes_bytes_and_resizes(world, gw, gateway):
    env, _, fake_pods = world
    await make_env(env)
    _, h = await person(gw, env)
    sid = await opened(gw, env, h)
    ws = Socket(gateway, sid, cookie(gw))
    assert (await ws.first())["type"] == "websocket.accept"
    await ws.send(data=b"ls\r")
    await ws.send(text=json.dumps({"type": "resize", "cols": 120, "rows": 40}))
    await ws.send(text=json.dumps({"type": "resize", "cols": 99999, "rows": 0}))
    await ws.send(text="not json")
    await ws.send(text=json.dumps({"type": "ping"}))
    await fake_pods.stream.queue.put(b"output")
    assert (await ws.first())["bytes"] == b"output"
    for _ in range(50):
        if len(fake_pods.stream.sizes) == 2:
            break
        await asyncio.sleep(0.01)
    assert fake_pods.stream.sent == [b"ls\r"] and fake_pods.stream.sizes == [(120, 40), (500, 1)]
    await ws.leave()
    assert fake_pods.stream.closed
    assert (await gw.get(f"/moks/api/terminal/sessions/{sid}")).json()["status"] == "ready"  # disconnecting is not ending


async def test_a_dropped_connection_can_come_back(world, gw, gateway):
    env, _, fake_pods = world
    await make_env(env)
    _, h = await person(gw, env)
    sid = await opened(gw, env, h)
    first = Socket(gateway, sid, cookie(gw))
    await first.first()
    await first.leave()
    fake_pods.stream = FakeStream()
    again = Socket(gateway, sid, cookie(gw))
    assert (await again.first())["type"] == "websocket.accept"
    await again.leave()
    s = (await gw.get(f"/moks/api/terminal/sessions/{sid}")).json()
    assert s["attach_count"] == 2 and len(await events("terminal.detach", sid)) == 2


async def test_a_second_window_takes_over(world, gw, gateway):
    env, _, fake_pods = world
    await make_env(env)
    _, h = await person(gw, env)
    sid = await opened(gw, env, h)
    first = Socket(gateway, sid, cookie(gw))
    await first.first()
    fake_pods.stream = FakeStream()
    second = Socket(gateway, sid, cookie(gw))
    await second.first()
    assert await first.close_code() == tapi.TAKEN_OVER
    await second.leave()


async def test_exiting_the_shell_ends_the_session(world, gw, gateway):
    env, _, fake_pods = world
    await make_env(env)
    _, h = await person(gw, env)
    sid = await opened(gw, env, h)
    ws = Socket(gateway, sid, cookie(gw))
    await ws.first()
    fake_pods.shell_alive = False
    await fake_pods.stream.queue.put(None)
    assert await ws.close_code() == tapi.ENDED
    assert (await gw.get(f"/moks/api/terminal/sessions/{sid}")).json()["end_reason"] == "shell_exited"


async def test_a_lost_connection_is_not_a_finished_shell(world, gw, gateway):
    env, _, fake_pods = world
    await make_env(env)
    _, h = await person(gw, env)
    sid = await opened(gw, env, h)
    ws = Socket(gateway, sid, cookie(gw))
    await ws.first()
    await fake_pods.stream.queue.put(None)
    assert await ws.close_code() == tapi.BROKEN  # the browser reconnects
    assert (await gw.get(f"/moks/api/terminal/sessions/{sid}")).json()["status"] == "ready"


async def test_expired_credentials_end_the_session_at_attach(world, gw, gateway):
    env, *_ = world
    await make_env(env)
    _, h = await person(gw, env)
    sid = await opened(gw, env, h)
    async with session_scope() as db:
        await db.execute(update(TerminalSession).where(TerminalSession.id == sid).values(credential_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    ws = Socket(gateway, sid, cookie(gw))
    assert await ws.close_code() == tapi.ENDED
    assert (await gw.get(f"/moks/api/terminal/sessions/{sid}")).json()["end_reason"] == "credentials_expired"


# ---- ending ---------------------------------------------------------------------------------

async def test_the_owner_stops_a_session_and_cleanup_deletes_the_pod(world, gw):
    env, _, fake_pods = world
    await make_env(env)
    _, h = await person(gw, env)
    sid = await opened(gw, env, h)
    out = (await gw.post(f"/moks/api/terminal/sessions/{sid}/stop", headers=h)).json()
    assert out["status"] == "ended" and out["end_reason"] == "stopped"
    await service.reap(fake_pods)
    assert f"moks-term-{str(sid)[:8]}" in fake_pods.deleted
    async with session_scope() as db:
        assert (await db.get(TerminalSession, sid)).pod_deleted_at
    assert await events("terminal.cleanup", sid)


async def test_someone_else_cannot_stop_a_session(world, gw):
    env, *_ = world
    await make_env(env)
    _, h = await person(gw, env)
    sid = await opened(gw, env, h)
    _, h2 = await person(gw, env)
    assert (await gw.post(f"/moks/api/terminal/sessions/{sid}/stop", headers=h2)).status_code == 404


async def test_an_admin_revokes_and_an_attached_shell_is_cut_off(world, gw, gateway):
    env, *_ = world
    await make_env(env)
    _, h = await person(gw, env)
    sid = await opened(gw, env, h)
    ws = Socket(gateway, sid, cookie(gw))
    await ws.first()
    _, admin = await person(gw, env, ["admin"])
    out = (await gw.post(f"/moks/api/terminal/sessions/{sid}/stop", headers=admin)).json()
    assert out["end_reason"] == "revoked"
    assert await ws.close_code() == tapi.ENDED
    assert (await events("terminal.end", sid))[0].actor_email.endswith("@example.com")


@pytest.mark.parametrize("field,reason", [("credential_expires_at", "credentials_expired"), ("expires_at", "max_lifetime"), ("last_input_at", "idle")])
async def test_timeouts_end_a_session_and_delete_its_pod(world, gw, field, reason):
    env, _, fake_pods = world
    await make_env(env)
    _, h = await person(gw, env)
    sid = await opened(gw, env, h)
    async with session_scope() as db:
        await db.execute(update(TerminalSession).where(TerminalSession.id == sid).values({field: datetime.now(timezone.utc) - timedelta(hours=2)}))
    await service.reap(fake_pods)
    s = (await gw.get(f"/moks/api/terminal/sessions/{sid}")).json()
    assert s["status"] == "ended" and s["end_reason"] == reason and s["pod"] in fake_pods.deleted


async def test_output_alone_does_not_keep_a_session_alive(world, gw, gateway):
    env, _, fake_pods = world
    await make_env(env)
    _, h = await person(gw, env)
    sid = await opened(gw, env, h)
    async with session_scope() as db:
        before = (await db.get(TerminalSession, sid)).last_input_at
    ws = Socket(gateway, sid, cookie(gw))
    await ws.first()
    await fake_pods.stream.queue.put(b"tail -f keeps printing")
    await ws.first()
    await ws.send(text=json.dumps({"type": "ping"}))
    await ws.leave()
    async with session_scope() as db:
        assert (await db.get(TerminalSession, sid)).last_input_at == before


async def test_removing_the_role_ends_the_session(world, gw):
    env, _, fake_pods = world
    await make_env(env)
    email, h = await person(gw, env)
    sid = await opened(gw, env, h)
    async with session_scope() as db:
        uid = (await db.execute(select(User.id).where(User.email == email))).scalar_one()
        await db.execute(delete(RoleBinding).where(RoleBinding.user_id == uid))
    await service.reap(fake_pods)
    async with session_scope() as db:
        assert (await db.get(TerminalSession, sid)).end_reason == "access_removed"


async def test_deactivating_the_user_ends_the_session(world, gw):
    env, _, fake_pods = world
    await make_env(env)
    email, h = await person(gw, env)
    sid = await opened(gw, env, h)
    async with session_scope() as db:
        await db.execute(update(User).where(User.email == email).values(is_active=False))
    await service.reap(fake_pods)
    async with session_scope() as db:
        assert (await db.get(TerminalSession, sid)).end_reason == "access_removed"


async def test_signing_out_ends_the_session(world, gw):
    env, _, fake_pods = world
    await make_env(env)
    _, h = await person(gw, env)
    sid = await opened(gw, env, h)
    async with session_scope() as db:
        row = await db.get(TerminalSession, sid)
        await db.execute(update(Session).where(Session.id == row.login_session).values(revoked_at=datetime.now(timezone.utc)))
    await service.reap(fake_pods)
    async with session_scope() as db:
        assert (await db.get(TerminalSession, sid)).end_reason == "logged_out"


async def test_a_pod_nobody_owns_is_deleted(world, gw):
    env, _, fake_pods = world
    fake_pods.existing["moks-term-orphan"] = str(uuid.uuid4())
    await service.reap(fake_pods)
    assert "moks-term-orphan" in fake_pods.deleted


async def test_a_session_whose_pod_vanished_is_closed(world, gw):
    env, _, fake_pods = world
    await make_env(env)
    _, h = await person(gw, env)
    sid = await opened(gw, env, h)
    fake_pods.existing.clear()
    await service.reap(fake_pods)
    assert (await gw.get(f"/moks/api/terminal/sessions/{sid}")).json()["end_reason"] == "pod_lost"


async def test_a_session_stuck_starting_fails(world, gw):
    env, fake_aws, fake_pods = world
    await make_env(env)
    _, h = await person(gw, env)
    r = await gw.post("/moks/api/terminal/sessions", json=body(env), headers=h)
    await settle()
    sid = uuid.UUID(r.json()["id"])
    async with session_scope() as db:
        await db.execute(update(TerminalSession).where(TerminalSession.id == sid).values(status="starting", created_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    await service.reap(fake_pods)
    assert (await gw.get(f"/moks/api/terminal/sessions/{sid}")).json()["status"] == "failed"


# ---- suggestions ----------------------------------------------------------------------------

async def _llm(monkeypatch, text, seen):
    from moks.api import querygen

    async def fake(request, cfg, system, user, uid, needs_query=True):
        seen.append(system + user)
        return json.loads(text)

    async def config(db):
        class C:
            provider, model = "test", "m"
        return C()
    monkeypatch.setattr(querygen, "draft", fake)
    monkeypatch.setattr(querygen, "chat_config", config)


async def test_suggestions_never_run_and_never_include_terminal_contents(world, gw, client, monkeypatch):
    env, _, fake_pods = world
    await make_env(env)
    seen = []
    await _llm(monkeypatch, json.dumps({"suggestions": [
        {"command": "aws ec2 describe-instances --region us-east-1", "explanation": "Lists instances."},
        {"command": "true\nrm -rf /tmp/x", "explanation": "A newline would run on paste."},
        {"command": "ls\u001b[201~", "explanation": "An escape could leave a bracketed paste."}]}), seen)
    _, h = await person(gw, env)
    sid = await opened(gw, env, h)
    client.cookies.set("moks_session", cookie(gw))
    ws_output = "password=hunter2 from the terminal"
    await fake_pods.stream.queue.put(ws_output.encode())
    r = await client.post(f"/moks/api/assist/terminal/{sid}", json={"question": "which instances are running?"}, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert [s["command"] for s in out["suggestions"]] == ["aws ec2 describe-instances --region us-east-1"] and out["executed"] is False
    assert "hunter2" not in seen[0] and "which instances" in seen[0] and "us-east-1" in seen[0]
    assert fake_pods.stream.sent == []


async def test_suggestions_are_for_the_owner_of_a_live_session(world, gw, client, monkeypatch):
    env, *_ = world
    await make_env(env)
    await _llm(monkeypatch, '{"suggestions": []}', [])
    _, h = await person(gw, env)
    sid = await opened(gw, env, h)
    _, h2 = await person(gw, env)
    client.cookies.set("moks_session", cookie(gw))
    assert (await client.post(f"/moks/api/assist/terminal/{sid}", json={"question": "anything"}, headers=h2)).status_code == 404


# ---- configuration --------------------------------------------------------------------------

@pytest.mark.parametrize("row", [
    {"name": "x", "environment": "e", "account": "123", "roleArn": ROLE, "regions": ["us-east-1"]},
    {"name": "x", "environment": "e", "account": "999988887777", "roleArn": ROLE, "regions": ["us-east-1"]},
    {"name": "x", "environment": "e", "account": "111122223333", "roleArn": ROLE, "regions": []},
    {"name": "x", "environment": "e", "account": "111122223333", "roleArn": ROLE, "regions": ["moon-1"]},
    {"name": "x", "environment": "", "account": "111122223333", "roleArn": ROLE, "regions": ["us-east-1"]},
    {"name": "x", "environment": "e", "account": "111122223333", "roleArn": "arn:aws:iam::111122223333:user/u", "regions": ["us-east-1"]},
])
def test_a_bad_profile_stops_the_gateway(row):
    with pytest.raises(tprofiles.ProfileError):
        tprofiles.parse(json.dumps([row]))


def test_source_identity_is_a_valid_aws_value():
    assert taws.source_identity("a b/c@example.com") == "a-b-c@example.com"
    assert len(taws.source_identity("x" * 200)) == 64


def test_the_gateway_serves_no_portal_route(gateway):
    paths = {getattr(r, "path", "") for r in gateway.routes}
    assert {p for p in paths if p.startswith("/moks")} == {p for p in paths if p.startswith("/moks/api/terminal")}
