"""The real MOKS API and worker, serving the built portal at /moks, with fixture connectors instead of a cluster.

Playwright starts this (see playwright.config.ts). Everything here is a FIXTURE, never mk4: Kubernetes, Prometheus
and Loki answer from backend/tests/portal_fixtures.py; the cost model is the real one with a fixed monthly price,
like mk4's. Investigations run in rules mode (no LLM provider is configured), through the real orchestrator.

  MOKS_E2E_PG    admin URL of a PostgreSQL server where this user may create databases
                 (default postgresql://moks@localhost:55432/postgres)
  PORT           default 8099

Each start uses a fresh database, dropped on exit.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

ADMIN_URL = os.environ.get("MOKS_E2E_PG", "postgresql://moks@localhost:55432/postgres")
DB = f"moks_e2e_{uuid.uuid4().hex[:8]}"
PORT = int(os.environ.get("PORT", "8099"))

os.environ.update({
    "MOKS_ENV": "development", "MOKS_AUTH_MODE": "dev", "MOKS_COOKIE_SECURE": "false",
    "MOKS_PUBLIC_URL": f"http://127.0.0.1:{PORT}/moks",
    "MOKS_DATABASE_URL": ADMIN_URL.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://") + f"/{DB}",
    "MOKS_STATIC_DIR": str(ROOT / "frontend" / "dist"), "MOKS_EMBEDDED_WORKER": "true",
    "MOKS_BOOTSTRAP_ADMINS": "sre@moks.local", "MOKS_METRICS_PORT": "0",
    "MOKS_RATE_INVESTIGATIONS_PER_MINUTE": "1000", "MOKS_RATE_LOGIN_PER_MINUTE": "1000",
})
os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)

SEED = """
environments:
  - name: mk4
    display_name: Minikube mk4 (fixture)
    kind: minikube
    is_production: false
    allowed_namespaces: ["*"]
    denied_namespaces: []
    max_query_window_minutes: 360
    connectors:
      - {type: kubernetes, name: fixture-k8s, config: {}}
      - {type: prometheus, name: fixture-prometheus, config: {base_url: "http://fixture.invalid"}}
      - {type: loki, name: fixture-loki, config: {base_url: "http://fixture.invalid"}}
      - {type: cost_model, name: mk4-fixed, config: {monthly_cost_usd: 60, cpu_share: 0.5, currency: USD}}
  - name: staging
    display_name: Staging (fixture)
    kind: minikube
    is_production: true
    allowed_namespaces: ["*"]
    denied_namespaces: []
    max_query_window_minutes: 360
    connectors:
      - {type: kubernetes, name: staging-k8s, config: {}}
"""


async def _db(sql: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(ADMIN_URL)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def _drop_stale() -> None:
    """Databases of earlier runs that were killed before they could drop their own. One still in use is left
    alone: without FORCE, DROP DATABASE refuses while anything is connected."""
    import asyncpg

    conn = await asyncpg.connect(ADMIN_URL)
    try:
        for (name,) in await conn.fetch("SELECT datname FROM pg_database WHERE datname LIKE 'moks_e2e_%'"):
            try:
                await conn.execute(f'DROP DATABASE "{name}"')
            except asyncpg.PostgresError:
                pass
    finally:
        await conn.close()


def main() -> None:
    asyncio.run(_drop_stale())
    asyncio.run(_db(f'CREATE DATABASE "{DB}"'))

    from moks import cli
    from moks.connectors import registry
    from tests.portal_fixtures import FIXTURE_TYPES

    cli.migrate()
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(SEED)

    async def seed() -> None:
        from moks import db

        await cli._seed(f.name, False)
        # sre@ is the admin; viewer@ may read one namespace of one environment and nothing in Admin.
        await cli._create_user("viewer@moks.local", None, ["viewer:mk4:shop"])
        await db.engine().dispose()  # bound to this event loop; the server opens its own
        db._engine = None

    asyncio.run(seed())
    registry.CONNECTOR_TYPES.update(FIXTURE_TYPES)

    import uvicorn

    try:
        uvicorn.run("moks.main:create_app", factory=True, host="127.0.0.1", port=PORT, log_level="warning", access_log=False)
    finally:
        asyncio.run(_db(f'DROP DATABASE IF EXISTS "{DB}" WITH (FORCE)'))


if __name__ == "__main__":
    main()
