"""moks command line.

  moks migrate                       apply database migrations (advisory-locked; safe from many pods)
  moks seed FILE [--upsert]          create environments/connectors/LLM configs from YAML
  moks serve                         run the API (uvicorn)
  moks worker                        run the investigation worker
  moks create-user EMAIL [--password-stdin] [--grant ROLE[:ENV[:NS]] ...]
  moks grant EMAIL ROLE[:ENV[:NS]]   add a role binding
  moks verify-audit                  recompute the audit hash chain
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path
from typing import Any

MIGRATION_LOCK = 7273111


def _alembic_config():
    from alembic.config import Config

    here = Path(__file__).resolve().parent
    cfg = Config()
    cfg.set_main_option("script_location", str(here / "migrations"))
    return cfg


def migrate() -> None:
    """Run Alembic under a PostgreSQL advisory lock so concurrent pods do not race."""
    import asyncpg
    from alembic import command

    from .config import get_settings

    url = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def locked(fn):
        conn = await asyncpg.connect(url)
        try:
            await conn.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK)
            await asyncio.to_thread(fn)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK)
            await conn.close()

    asyncio.run(locked(lambda: command.upgrade(_alembic_config(), "head")))
    print("migrations applied")


async def _seed(path: str, upsert: bool) -> None:
    import yaml
    from sqlalchemy import select

    from . import audit
    from .db import session_scope
    from .models import Connector, Environment, LLMConfig

    data: dict[str, Any] = yaml.safe_load(Path(path).read_text()) or {}
    async with session_scope() as db:
        for e in data.get("environments", []):
            connectors = e.pop("connectors", [])
            env = (await db.execute(select(Environment).where(Environment.name == e["name"]))).scalar_one_or_none()
            if env is None:
                env = Environment(**e)
                db.add(env)
                await db.flush()
                print(f"environment {env.name}: created")
            elif upsert:
                for k, v in e.items():
                    setattr(env, k, v)
                print(f"environment {env.name}: updated")
            for c in connectors:
                row = (await db.execute(select(Connector).where(Connector.environment_id == env.id, Connector.name == c["name"]))).scalar_one_or_none()
                if row is None:
                    db.add(Connector(environment_id=env.id, **c))
                    print(f"  connector {c['name']}: created")
                elif upsert:
                    for k, v in c.items():
                        setattr(row, k, v)
                    print(f"  connector {c['name']}: updated")
        for l in data.get("llm", []):
            row = (await db.execute(select(LLMConfig).where(LLMConfig.name == l["name"]))).scalar_one_or_none()
            if row is None:
                if l.get("is_active"):
                    for other in (await db.execute(select(LLMConfig).where(LLMConfig.purpose == l.get("purpose", "chat"), LLMConfig.is_active.is_(True)))).scalars():
                        other.is_active = False
                    await db.flush()
                db.add(LLMConfig(**l))
                print(f"llm {l['name']}: created")
            elif upsert:
                for k, v in l.items():
                    setattr(row, k, v)
                print(f"llm {l['name']}: updated")
        await audit.record(db, action="config.seed", details={"file": os.path.basename(path), "upsert": upsert})


async def _create_user(email: str, password: str | None, grants: list[str]) -> None:
    from sqlalchemy import select

    from . import audit
    from .auth.routes import hasher
    from .config import get_settings
    from .db import session_scope
    from .models import RoleBinding, User
    from .rbac import parse_grant

    mode = get_settings().auth_mode
    async with session_scope() as db:
        user = (await db.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none()
        if user is None:
            if mode == "oidc":
                raise SystemExit("auth mode is oidc: the user is created at first sign-in; run `moks grant` afterwards")
            user = User(email=email.lower(), display_name=email.split("@")[0], auth_provider=mode)
            db.add(user)
            await db.flush()
            print(f"user {email}: created")
        if password:
            if len(password) < 12:
                raise SystemExit("password must be at least 12 characters")
            user.password_hash = hasher.hash(password)
            print(f"user {email}: password set")
        for g in grants:
            b = parse_grant(g)
            exists = (await db.execute(select(RoleBinding).where(RoleBinding.user_id == user.id, RoleBinding.role == b.role,
                                                                 RoleBinding.environment == b.environment, RoleBinding.namespace == b.namespace))).scalar_one_or_none()
            if not exists:
                db.add(RoleBinding(user_id=user.id, role=b.role, environment=b.environment, namespace=b.namespace, source="cli"))
                print(f"user {email}: granted {b.role} on {b.environment}/{b.namespace}")
        await audit.record(db, action="user.cli", actor_email="cli", resource_type="user", resource_id=str(user.id),
                           details={"email": email, "grants": grants, "password_set": bool(password)})


async def _grant(email: str, grant: str) -> None:
    from sqlalchemy import select

    from .db import session_scope
    from .models import User

    async with session_scope() as db:
        if (await db.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none() is None:
            raise SystemExit(f"no user {email}; with OIDC they must sign in once first")
    await _create_user(email, None, [grant])


async def _verify() -> None:
    from . import audit
    from .db import session_scope

    async with session_scope() as db:
        result = await audit.verify_chain(db)
    print(result)
    if not result["ok"]:
        raise SystemExit(1)


def serve() -> None:
    import uvicorn

    uvicorn.run("moks.main:create_app", factory=True, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                proxy_headers=True, forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "*"),
                log_config=None, access_log=False, timeout_graceful_shutdown=20)


def worker() -> None:
    import signal

    from .config import get_settings
    from .observability import setup_logging, setup_tracing, start_metrics_server
    from .orchestrator.worker import run_worker

    s = get_settings()
    setup_logging(s.log_level)
    setup_tracing(s.otel_service_name + "-worker")
    start_metrics_server(s.metrics_port)

    async def main():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run_worker(stop)

    asyncio.run(main())


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="moks")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sp = sub.add_parser("seed")
    sp.add_argument("file")
    sp.add_argument("--upsert", action="store_true", help="overwrite existing rows with the file's values")
    sp.add_argument("--if-present", action="store_true", help="do nothing if the file does not exist")
    sub.add_parser("serve")
    sub.add_parser("worker")
    cu = sub.add_parser("create-user")
    cu.add_argument("email")
    cu.add_argument("--password-stdin", action="store_true", help="read the password from stdin (local auth mode)")
    cu.add_argument("--password-prompt", action="store_true")
    cu.add_argument("--grant", action="append", default=[], help="role[:environment[:namespace]]")
    g = sub.add_parser("grant")
    g.add_argument("email")
    g.add_argument("grant")
    sub.add_parser("verify-audit")
    a = p.parse_args(argv)

    if a.cmd == "migrate":
        migrate()
    elif a.cmd == "seed":
        if a.if_present and not Path(a.file).exists():
            print(f"{a.file} not present; nothing to seed")
            return
        asyncio.run(_seed(a.file, a.upsert))
    elif a.cmd == "serve":
        serve()
    elif a.cmd == "worker":
        worker()
    elif a.cmd == "create-user":
        pw = None
        if a.password_stdin:
            pw = sys.stdin.readline().rstrip("\n")
        elif a.password_prompt:
            pw = getpass.getpass("Password: ")
        asyncio.run(_create_user(a.email, pw, a.grant))
    elif a.cmd == "grant":
        asyncio.run(_grant(a.email, a.grant))
    elif a.cmd == "verify-audit":
        asyncio.run(_verify())


if __name__ == "__main__":
    main()
