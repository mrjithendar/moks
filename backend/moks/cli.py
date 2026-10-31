"""moks command line.

  moks migrate                       apply database migrations (advisory-locked; safe from many pods)
  moks db-grants                     create per-component database roles and apply their grants
  moks wait-schema                   wait until the database schema is ready for this image
  moks seed FILE [--upsert]          create environments/connectors/LLM configs from YAML
  moks serve                         run the API (uvicorn)
  moks worker                        run the investigation worker
  moks create-user EMAIL [--password-stdin] [--grant ROLE[:ENV[:NS]] ...]
  moks grant EMAIL ROLE[:ENV[:NS]]   add a role binding
  moks verify-audit                  recompute the audit hash chain
  moks executor                      run the action/diagnostics executor (write credentials)
  moks terminal                      run the terminal gateway (pods in the terminal namespace only)
  moks eval DATASET [--mode rules|llm]  run an evaluation dataset and print scores
  moks reindex                       rebuild the knowledge index for the active embedding model
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


def db_grants() -> None:
    """Create the per-component roles and apply their grants. Run by the migration owner."""
    import asyncpg

    from . import db_roles
    from .config import get_settings

    url = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def run():
        conn = await asyncpg.connect(url)
        try:
            return await db_roles.apply(conn, await conn.fetchval("SELECT current_database()"))
        finally:
            await conn.close()

    changed = asyncio.run(run())
    print(f"grants applied; passwords set for: {', '.join(changed) or 'none'}")


def wait_schema(timeout: int = 300) -> None:
    """Runtime pods hold no schema rights. They wait until the database is at least what they need.
    A revision this image does not know is newer, which expand/contract migrations allow."""
    import time

    import asyncpg
    from alembic.script import ScriptDirectory

    from .config import get_settings

    script = ScriptDirectory.from_config(_alembic_config())
    known, head = {r.revision for r in script.walk_revisions()}, script.get_current_head()
    order = [r.revision for r in script.walk_revisions()]
    url = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def current():
        conn = await asyncpg.connect(url)
        try:
            return await conn.fetchval("SELECT version_num FROM alembic_version")
        finally:
            await conn.close()

    deadline = time.monotonic() + timeout
    while True:
        try:
            at = asyncio.run(current())
        except Exception as exc:
            at, reason = None, type(exc).__name__
        else:
            reason = "no version yet"
        if at and (at not in known or order.index(at) <= order.index(head)):
            print(f"schema ready at {at}")
            return
        if time.monotonic() > deadline:
            raise SystemExit(f"the schema did not reach {head} within {timeout}s (at {at or reason})")
        time.sleep(3)


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
        from .api.environments import sync_automation_binding
        from .models import KnowledgeSource

        for ks in data.get("knowledge_sources", []):
            row = (await db.execute(select(KnowledgeSource).where(KnowledgeSource.name == ks["name"]))).scalar_one_or_none()
            if row is None:
                db.add(KnowledgeSource(**{"shared_globally": ks.get("scope_environment", "*") == "*" and ks.get("scope_namespace", "*") == "*", **ks}))
                print(f"knowledge source {ks['name']}: created (first sync is scheduled by the worker)")
            elif upsert:
                for k, v in ks.items():
                    setattr(row, k, v)

        for e in data.get("environments", []):
            env = (await db.execute(select(Environment).where(Environment.name == e["name"]))).scalar_one()
            await sync_automation_binding(db, env.name, env.auto_investigate or env.detection_enabled)
        for l in data.get("llm", []):
            row = (await db.execute(select(LLMConfig).where(LLMConfig.name == l["name"]))).scalar_one_or_none()
            if row is None:
                # The seed runs on every pod start, so it must never override a choice made in the portal.
                active = (await db.execute(select(LLMConfig).where(LLMConfig.purpose == l.get("purpose", "chat"), LLMConfig.is_active.is_(True)))).scalars().first()
                db.add(LLMConfig(**{**l, "is_active": bool(l.get("is_active")) and active is None}))
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


def terminal() -> None:
    import uvicorn

    from .config import get_settings

    if get_settings().process_role != "terminal":
        raise SystemExit("moks terminal needs MOKS_PROCESS_ROLE=terminal")
    uvicorn.run("moks.terminal.app:create_app", factory=True, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                proxy_headers=True, forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "*"), ws_max_size=1_048_576,
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


def executor() -> None:
    import os
    import signal

    os.environ["MOKS_PROCESS_ROLE"] = "executor"
    from .config import get_settings

    get_settings.cache_clear()
    from .actions.executor import run_executor
    from .observability import setup_logging, setup_tracing, start_metrics_server

    s = get_settings()
    setup_logging(s.log_level)
    setup_tracing(s.otel_service_name + "-executor")
    start_metrics_server(s.metrics_port)

    async def main():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run_executor(stop)

    asyncio.run(main())


async def _eval(dataset: str, mode: str) -> None:
    from .evals import runner

    rid = await runner.start(dataset, mode)
    summary = await runner.execute(rid)
    from .db import session_scope
    from .models import EvalRun

    async with session_scope() as db:
        run = await db.get(EvalRun, rid)
        for r in run.results:
            failed = [k for k, v in r["checks"].items() if not v]
            print(f"{r['case']:40} {r['score']:.2f}  {'FAILED: ' + ', '.join(failed) if failed else 'ok'}")
    print(f"score {summary['score']:.3f} over {summary['cases']} cases (run {rid})")


async def _reindex() -> None:
    from .db import session_scope
    from .rag import service

    async with session_scope() as db:
        print(await service.reindex(db))


async def _backup_run(dump: str, manifest_path: str | None = None) -> None:
    from datetime import datetime, timezone

    from sqlalchemy import text

    from . import backup
    from .db import session_scope
    from .models import BackupRun

    started = datetime.now(timezone.utc)
    env = os.environ
    result: dict[str, Any] = {}
    counts: dict[str, int] = {}
    size = None
    error = None
    try:
        bucket = env.get("MOKS_BACKUP_BUCKET")
        if not bucket:
            raise backup.BackupError("MOKS_BACKUP_BUCKET is not set")
        key = backup.load_key()
        if Path(dump + ".failed").exists():
            raise backup.BackupError("pg_dump failed")
        async with session_scope() as db:
            size = (await db.execute(text("SELECT pg_database_size(current_database())"))).scalar_one()
            if not manifest_path:  # read after the dump, so only good enough for manual runs
                counts = await backup.table_counts(db)
                version = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        snapshot: dict[str, Any] = {"tables": counts, "migration": version, "counts_source": "live"} if not manifest_path else {**json.loads(Path(manifest_path).read_text()), "counts_source": "snapshot"}
        counts = snapshot["tables"]
        retention = backup.Retention(int(env.get("MOKS_BACKUP_KEEP_HOURLY", 48)), int(env.get("MOKS_BACKUP_KEEP_DAILY", 30)), int(env.get("MOKS_BACKUP_KEEP_MONTHLY", 12)))
        store = backup.Store(bucket, env.get("MOKS_BACKUP_ENDPOINT"), env.get("MOKS_BACKUP_REGION"))
        result = backup.publish(Path(dump), store, env.get("MOKS_BACKUP_PREFIX", "moks"), key, {**snapshot, "database_bytes": size}, started, retention)
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:500]}"
    async with session_scope() as db:
        db.add(BackupRun(started_at=started, finished_at=datetime.now(timezone.utc), status="failed" if error else "success", object_key=result.get("key"),
                         size_bytes=result.get("bytes"), sha256=result.get("sha256"), database_bytes=size, tables=counts or None, error=error))
    if error:
        print(f"backup failed: {error}", file=sys.stderr)
        raise SystemExit(1)
    print(f"backup ok: {result['key']} ({result['bytes']} bytes), {len(counts)} tables, {result['pruned']} old copies pruned")


async def _table_counts_at(url: str) -> dict[str, int]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from . import backup

    engine = create_async_engine(url)
    try:
        async with async_sessionmaker(engine)() as db:
            return await backup.table_counts(db)
    finally:
        await engine.dispose()


async def _backup_compare() -> None:
    """URLs come from the environment so passwords never appear in a process list."""
    from . import backup

    drift = backup.live_drift(await _table_counts_at(os.environ["MOKS_COMPARE_SOURCE_URL"]), await _table_counts_at(os.environ["MOKS_COMPARE_TARGET_URL"]))
    print(json.dumps({"note": "drift from a live source is expected; `moks backup drill` proves integrity", "drift": drift}, indent=1))


async def _backup_drill(key: str | None) -> None:
    """URLs come from the environment. MOKS_DRILL_ADMIN_URL needs CREATE DATABASE on a server the
    application is not using, or at least a maintenance database that is not the live one."""
    import tempfile

    from . import backup
    from .config import get_settings

    env = os.environ
    store = backup.Store(env["MOKS_BACKUP_BUCKET"], env.get("MOKS_BACKUP_ENDPOINT"), env.get("MOKS_BACKUP_REGION"))
    if key is None:
        prefix = env.get("MOKS_BACKUP_PREFIX", "moks").strip("/")
        found = sorted(k for k in store.keys(f"{prefix}/hourly/") if k.endswith(".dump.enc"))
        if not found:
            raise SystemExit("no backups found")
        key = found[-1]
    live = get_settings().database_url.rsplit("/", 1)[-1].split("?")[0]
    with tempfile.TemporaryDirectory() as work:
        report = await backup.restore_drill(store, key, backup.load_key(), env["MOKS_DRILL_ADMIN_URL"], live, Path(work))
    print(json.dumps(report, indent=1, default=str))
    if not report["ok"]:
        raise SystemExit(1)


def _backup_fetch(key: str | None, out: str) -> None:
    from . import backup

    env = os.environ
    store = backup.Store(env["MOKS_BACKUP_BUCKET"], env.get("MOKS_BACKUP_ENDPOINT"), env.get("MOKS_BACKUP_REGION"))
    if key is None:
        prefix = env.get("MOKS_BACKUP_PREFIX", "moks").strip("/")
        found = sorted(k for k in store.keys(f"{prefix}/hourly/") if k.endswith(".dump.enc"))
        if not found:
            raise SystemExit("no backups found")
        key = found[-1]
    sealed = Path(out + ".enc")
    store.download(key, sealed)
    with sealed.open("rb") as src, open(out, "wb") as dst:
        backup.decrypt_stream(src, dst, backup.load_key())
    sealed.unlink()
    print(f"{key} -> {out}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="moks")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sub.add_parser("db-grants")
    sub.add_parser("wait-schema")
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
    sub.add_parser("executor")
    sub.add_parser("terminal")
    ev = sub.add_parser("eval")
    ev.add_argument("dataset")
    ev.add_argument("--mode", choices=["rules", "llm"], default="rules")
    sub.add_parser("reindex")
    bk = sub.add_parser("backup").add_subparsers(dest="backup_cmd", required=True)
    br = bk.add_parser("run", help="encrypt a pg_dump file, upload it, verify the copy and apply retention")
    br.add_argument("--dump", required=True)
    br.add_argument("--manifest", help="the manifest written with the dump, from the same snapshot")
    bf = bk.add_parser("fetch", help="download and decrypt a backup (default: the newest)")
    bf.add_argument("--key")
    bf.add_argument("--out", required=True)
    bk.add_parser("compare", help="report drift between MOKS_COMPARE_SOURCE_URL and MOKS_COMPARE_TARGET_URL")
    bd = bk.add_parser("drill", help="restore a backup into a new isolated database, prove it, drop it")
    bd.add_argument("--key")
    a = p.parse_args(argv)

    if a.cmd == "migrate":
        migrate()
    elif a.cmd == "db-grants":
        db_grants()
    elif a.cmd == "wait-schema":
        wait_schema()
    elif a.cmd == "seed":
        if a.if_present and not Path(a.file).exists():
            print(f"{a.file} not present; nothing to seed")
            return
        asyncio.run(_seed(a.file, a.upsert))
    elif a.cmd == "terminal":
        terminal()
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
    elif a.cmd == "executor":
        executor()
    elif a.cmd == "eval":
        asyncio.run(_eval(a.dataset, a.mode))
    elif a.cmd == "reindex":
        asyncio.run(_reindex())
    elif a.cmd == "backup":
        if a.backup_cmd == "run":
            asyncio.run(_backup_run(a.dump, a.manifest))
        elif a.backup_cmd == "drill":
            asyncio.run(_backup_drill(a.key))
        elif a.backup_cmd == "fetch":
            _backup_fetch(a.key, a.out)
        else:
            asyncio.run(_backup_compare())


if __name__ == "__main__":
    main()
