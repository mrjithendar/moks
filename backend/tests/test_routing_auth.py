"""/moks base-path routing, SPA deep links, authentication, CSRF and sessions."""
from tests.conftest import login


async def test_base_path_redirects_to_trailing_slash(client):
    r = await client.get("/moks", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/moks/"


async def test_deep_links_serve_the_spa_shell(client):
    for path in ("/moks/", "/moks/investigations/1b2c", "/moks/admin/connectors", "/moks/login"):
        r = await client.get(path)
        assert r.status_code == 200, path
        assert "MOKS test shell" in r.text
        assert r.headers["cache-control"] == "no-cache"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


async def test_assets_are_served_and_missing_assets_are_404_not_html(client):
    ok = await client.get("/moks/assets/app.js")
    assert ok.status_code == 200 and "immutable" in ok.headers["cache-control"]
    missing = await client.get("/moks/assets/nope.js")
    assert missing.status_code == 404 and "MOKS test shell" not in missing.text


async def test_path_traversal_is_not_served(client):
    r = await client.get("/moks/..%2f..%2fconftest.py")
    assert "fixtures" not in r.text and "import" not in r.text


async def test_unknown_api_paths_return_json_404(client):
    r = await client.get("/moks/api/does-not-exist")
    assert r.status_code == 404 and r.json()["detail"] == "Not found"


async def test_api_docs_disabled_by_default(client):
    assert (await client.get("/moks/api/docs")).status_code == 404


async def test_probes_and_public_health(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert (await client.get("/readyz")).status_code == 200
    assert (await client.get("/moks/api/health")).json()["status"] == "ok"


async def test_unauthenticated_api_is_rejected(client):
    assert (await client.get("/moks/api/auth/me")).status_code == 401
    assert (await client.get("/moks/api/investigations")).status_code == 401


async def test_session_cookie_attributes(client):
    r = await client.post("/moks/api/auth/dev-login", json={"email": "cookie@example.com"})
    cookie = r.headers["set-cookie"]
    assert "moks_session=" in cookie and "HttpOnly" in cookie and "Path=/moks" in cookie and "SameSite=lax" in cookie


async def test_csrf_and_origin_are_required_for_mutations(client):
    headers = await login(client, "admin@example.com")
    body = {"name": "csrf-env", "kind": "minikube"}
    assert (await client.post("/moks/api/environments", json=body)).status_code == 403  # no token
    bad_origin = {**headers, "Origin": "https://evil.example"}
    assert (await client.post("/moks/api/environments", json=body, headers=bad_origin)).status_code == 403
    assert (await client.post("/moks/api/environments", json=body, headers=headers)).status_code == 201


async def test_new_users_have_no_access_until_granted(client):
    headers = await login(client, "nobody@example.com")
    me = (await client.get("/moks/api/auth/me")).json()
    assert me["roles"] == [] and me["permissions"] == []
    assert (await client.get("/moks/api/admin/users")).status_code == 403
    assert (await client.post("/moks/api/environments", json={"name": "x", "kind": "eks"}, headers=headers)).status_code == 403


async def test_bootstrap_admin_gets_admin(client):
    await login(client, "admin@example.com")
    me = (await client.get("/moks/api/auth/me")).json()
    assert {"role": "admin", "environment": "*", "namespace": "*"} in me["roles"]


async def test_logout_revokes_the_session(client):
    headers = await login(client, "bye@example.com")
    r = await client.post("/moks/api/auth/logout", headers=headers)
    assert r.json()["redirect"] == "/moks/login"
    assert (await client.get("/moks/api/auth/me")).status_code == 401


async def test_denials_are_audited(client):
    await login(client, "denied@example.com")
    await client.get("/moks/api/admin/users")
    headers = await login(client, "admin@example.com")
    events = (await client.get("/moks/api/admin/audit", params={"actor": "denied@example.com", "decision": "deny"})).json()["events"]
    assert any(e["action"] == "authz.deny" for e in events)


async def test_local_login_rejects_bad_password_and_rate_limits(client, monkeypatch):
    from moks.config import get_settings
    from moks.cli import _create_user

    monkeypatch.setattr(get_settings(), "auth_mode", "local")
    await _create_user("local@example.com", "a-very-long-password", ["viewer"])
    bad = await client.post("/moks/api/auth/login", json={"email": "local@example.com", "password": "wrong"})
    assert bad.status_code == 401
    ok = await client.post("/moks/api/auth/login", json={"email": "local@example.com", "password": "a-very-long-password"})
    assert ok.status_code == 200
    codes = [(await client.post("/moks/api/auth/login", json={"email": "local@example.com", "password": "x"})).status_code for _ in range(6)]
    assert 429 in codes


async def test_dev_login_refused_outside_dev_mode(client, monkeypatch):
    from moks.config import get_settings

    monkeypatch.setattr(get_settings(), "auth_mode", "oidc")
    assert (await client.post("/moks/api/auth/dev-login", json={"email": "x@example.com"})).status_code == 404


def test_dev_mode_refused_in_production(monkeypatch):
    import pytest
    from pydantic import ValidationError

    from moks.config import Settings

    with pytest.raises(ValidationError):
        Settings(env="production", auth_mode="dev", session_secret="x" * 40)
    with pytest.raises(ValidationError):
        Settings(env="production", auth_mode="local", session_secret="short")


def test_open_redirects_are_blocked():
    from moks.auth.routes import safe_next

    assert safe_next("/moks/investigations/1") == "/moks/investigations/1"
    for evil in ("https://evil.com", "//evil.com", "/moks/../x://", "/other", None, "/moks\\@evil"):
        assert safe_next(evil) == "/moks/"


def test_database_dependency_commits_before_the_response():
    """Depends(get_db) with the default request scope commits AFTER the response is sent, so a client
    could sign in and immediately get 401 (observed live). Every use must be scope="function"."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "moks"
    offenders = [str(p) for p in root.rglob("*.py") if re.search(r"Depends\(get_db\)", p.read_text())]
    assert offenders == []
