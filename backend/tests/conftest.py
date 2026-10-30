"""Test fixtures.

Tests need a PostgreSQL server (the audit chain, SKIP LOCKED claiming and JSONB are real
PostgreSQL features). MOKS_TEST_DATABASE_URL points at a server where the test user may
create databases; a fresh database is created for each run.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest

ADMIN_URL = os.environ.get("MOKS_TEST_DATABASE_URL", "postgresql://moks@localhost:55432/postgres")
DB_NAME = f"moks_test_{uuid.uuid4().hex[:8]}"
_base = ADMIN_URL.rsplit("/", 1)[0]

STATIC = Path(__file__).parent / "fixtures" / "static"

os.environ.update({
    "MOKS_ENV": "development",
    "MOKS_AUTH_MODE": "dev",
    "MOKS_DATABASE_URL": f"{_base.replace('postgresql://', 'postgresql+asyncpg://')}/{DB_NAME}",
    "MOKS_COOKIE_SECURE": "false",
    "MOKS_PUBLIC_URL": "http://testserver/moks",
    "MOKS_STATIC_DIR": str(STATIC),
    "MOKS_BOOTSTRAP_ADMINS": "admin@example.com",
    "MOKS_MAX_DURATION_SECONDS": "20",
    "MOKS_RATE_LOGIN_PER_MINUTE": "5",
    "MOKS_RATE_INVESTIGATIONS_PER_MINUTE": "1000",
    "MOKS_METRICS_PORT": "0",
})
os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)


@pytest.fixture(scope="session", autouse=True)
def database():
    import asyncpg

    async def create():
        conn = await asyncpg.connect(ADMIN_URL)
        await conn.execute(f'CREATE DATABASE "{DB_NAME}"')
        await conn.close()

    async def drop():
        conn = await asyncpg.connect(ADMIN_URL)
        await conn.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
        await conn.close()

    asyncio.run(create())
    from moks.cli import migrate

    migrate()
    yield
    asyncio.run(drop())


@pytest.fixture(scope="session")
def app(database):
    from moks.main import create_app

    return create_app()


@pytest.fixture
async def client(app):
    import httpx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


async def login(client, email: str) -> dict:
    """Dev-mode sign-in; returns headers carrying the CSRF token for unsafe requests."""
    r = await client.post("/moks/api/auth/dev-login", json={"email": email})
    assert r.status_code == 200, r.text
    me = (await client.get("/moks/api/auth/me")).json()
    return {"X-CSRF-Token": me["csrf_token"], "Origin": "http://testserver"}


async def make_user(email: str, grants: list[str]) -> uuid.UUID:
    from moks.cli import _create_user
    from moks.db import session_scope
    from moks.models import User
    from sqlalchemy import select

    await _create_user(email, None, grants)
    async with session_scope() as db:
        return (await db.execute(select(User.id).where(User.email == email))).scalar_one()


async def make_env(name: str, allowed=("*",), denied=(), connectors: list[dict] | None = None) -> uuid.UUID:
    from moks.db import session_scope
    from moks.models import Connector, Environment

    async with session_scope() as db:
        env = Environment(name=name, kind="minikube", allowed_namespaces=list(allowed), denied_namespaces=list(denied),
                          max_query_window_minutes=360)
        db.add(env)
        await db.flush()
        for c in connectors or []:
            db.add(Connector(environment_id=env.id, **c))
        return env.id
