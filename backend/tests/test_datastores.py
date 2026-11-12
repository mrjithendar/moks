"""PostgreSQL and Redis/Valkey clusters: wire protocol, read-only guarantees, findings, scope and discovery.

The Redis connector runs against an in-process RESP server (and a real redis-server when one is installed);
the PostgreSQL connector runs against the test PostgreSQL server as a role holding only pg_monitor.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from moks import datastores
from moks.connectors import registry
from moks.connectors.base import ConnectorError
from moks.connectors.postgres import PostgresConnector, normalize_sql
from moks.connectors.redis import RedisConnector, key_label, summarize_info
from moks.connectors.resp import encode, parse_client_list, parse_cluster_nodes, parse_info
from moks.models import Environment
from moks.rbac import Binding, Principal
from moks.scope import Scope, ScopeError
from moks.tools.builtin import REGISTRY
from moks.tools.contracts import ToolContext
from moks.tools.executor import ToolExecutor, tools_for_planner
from tests.conftest import ADMIN_URL, login, make_env

NOW = datetime.now(timezone.utc)

INFO = """# Server\r\nredis_version:7.2.4\r\nuptime_in_seconds:3600\r\n\r\n# Clients\r\nconnected_clients:9000\r\nblocked_clients:2\r\nmaxclients:10000\r
# Memory\r\nused_memory:960000000\r\nused_memory_rss:1100000000\r\nmaxmemory:1000000000\r\nmaxmemory_policy:noeviction\r\nmem_fragmentation_ratio:1.15\r
# Persistence\r\nloading:0\r\nrdb_last_bgsave_status:err\r\naof_enabled:0\r\n\r\n# Stats\r\ninstantaneous_ops_per_sec:120\r\nkeyspace_hits:90\r\nkeyspace_misses:10\r
evicted_keys:0\r\nrejected_connections:3\r\n\r\n# Replication\r\nrole:master\r\nconnected_slaves:1\r\nmaster_repl_offset:5000\r
slave0:ip=10.0.0.9,port=6379,state=online,offset=4000,lag=30\r\n\r\n# Keyspace\r\ndb0:keys=12,expires=3,avg_ttl=100\r\n"""


class FakeRedisServer:
    """Answers the read commands MOKS sends, records every command, and refuses what an ACL would refuse."""

    def __init__(self, *, password: str | None = "pw", info: str = INFO, denied: tuple[str, ...] = (), cluster: bool = False):
        self.password, self.info, self.denied, self.cluster = password, info, denied, cluster
        self.commands: list[list[str]] = []
        self.keys = {"session:alice@example.com": 5000, "user:42": 80, "plain": 10}
        self.server: asyncio.base_events.Server | None = None
        self.writers: list[asyncio.StreamWriter] = []
        self.port = 0

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        for w in self.writers:  # wait_closed() waits for open connections from Python 3.12
            w.close()
        self.server.close()
        await self.server.wait_closed()

    async def _read(self, r: asyncio.StreamReader) -> list[str] | None:
        line = await r.readline()
        if not line:
            return None
        n = int(line[1:])
        out = []
        for _ in range(n):
            size = int((await r.readline())[1:])
            out.append((await r.readexactly(size + 2))[:-2].decode())
        return out

    async def _client(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        authed = self.password is None
        self.writers.append(w)
        while True:
            try:
                cmd = await self._read(r)
            except (ConnectionError, asyncio.IncompleteReadError, ValueError):
                break
            if cmd is None:
                break
            self.commands.append(cmd)
            w.write(self._answer(cmd, authed))
            if cmd[0].upper() == "AUTH":
                authed = cmd[-1] == self.password
            await w.drain()
        w.close()

    def _answer(self, cmd: list[str], authed: bool) -> bytes:
        name = cmd[0].upper()
        sub = cmd[1].upper() if len(cmd) > 1 else ""
        bulk = lambda s: b"$%d\r\n%s\r\n" % (len(s.encode()), s.encode())  # noqa: E731
        arr = lambda items: b"*%d\r\n" % len(items) + b"".join(items)  # noqa: E731
        if name == "AUTH":
            return b"+OK\r\n" if cmd[-1] == self.password else b"-WRONGPASS invalid username-password pair\r\n"
        if not authed:
            return b"-NOAUTH Authentication required.\r\n"
        if name in self.denied or f"{name} {sub}" in self.denied:
            return b"-NOPERM this user has no permissions to run the '%s' command\r\n" % name.lower().encode()
        if name == "INFO":
            return bulk(self.info)
        if name == "CONFIG":
            return arr([bulk(cmd[2]), bulk({"maxmemory": "1000000000", "maxmemory-policy": "noeviction"}.get(cmd[2], "0"))])
        if name == "SLOWLOG":
            return arr([arr([b":7\r\n", b":1700000000\r\n", b":25000\r\n", arr([bulk("GET"), bulk("session:alice@example.com")]), bulk("10.0.0.5:4000"), bulk("api")])])
        if name == "LATENCY":
            return arr([arr([bulk("command"), b":1700000000\r\n", b":30\r\n", b":250\r\n"])])
        if name == "CLIENT":
            return bulk("id=1 addr=10.0.0.5:4000 name=api age=10 idle=0 flags=N db=0 omem=0 cmd=get\nid=2 addr=10.0.0.6:4001 name= age=99 idle=90 flags=b db=0 omem=2048 cmd=blpop\n")
        if name == "SCAN":
            return arr([bulk("0"), arr([bulk(k) for k in self.keys])])
        if name == "MEMORY":
            return b":%d\r\n" % self.keys.get(cmd[2], 0)
        if name == "CLUSTER" and sub == "INFO":
            return bulk("cluster_state:fail\r\ncluster_slots_assigned:16384\r\ncluster_slots_ok:16000\r\ncluster_slots_pfail:0\r\ncluster_slots_fail:384\r\ncluster_known_nodes:2\r\ncluster_size:2\r\n")
        if name == "CLUSTER" and sub == "NODES":
            return bulk(f"aaaa 127.0.0.1:{self.port}@16379 myself,master - 0 0 1 connected 0-8191\n"
                        f"bbbb 127.0.0.1:1@16380 master,fail - 0 0 2 disconnected 8192-16383\n")
        return b"-ERR unknown command '%s'\r\n" % name.lower().encode()


@pytest.fixture
def redis_cred(monkeypatch):
    monkeypatch.setenv("MOKS_CRED_REDIS_TEST", "pw")
    return {"type": "env", "name": "MOKS_CRED_REDIS_TEST"}


# ---- wire protocol ----------------------------------------------------------------------------------

def test_resp_encoding_and_parsers():
    assert encode("GET", "k") == b"*2\r\n$3\r\nGET\r\n$1\r\nk\r\n"
    info = parse_info(INFO)
    assert info["memory"]["maxmemory"] == 1_000_000_000 and info["keyspace"]["db0"] == {"keys": 12, "expires": 3, "avg_ttl": 100}
    assert info["replication"]["slave0"]["state"] == "online" and info["server"]["redis_version"] == "7.2.4"
    clients = parse_client_list("id=1 addr=1.2.3.4:5 name=x idle=3 flags=b\n")
    assert clients[0]["idle"] == 3 and clients[0]["flags"] == "b"
    nodes = parse_cluster_nodes("abc 10.0.0.1:6379@16379 master,fail - 0 0 1 connected 0-100\n")
    assert nodes[0]["failed"] and nodes[0]["port"] == 6379 and nodes[0]["role"] == "master"


async def test_redis_overview_findings_and_hashing(redis_cred):
    async with FakeRedisServer() as srv:
        r = RedisConnector({"host": "127.0.0.1", "port": srv.port, "username": "moks"}, redis_cred)
        assert "Redis 7.2.4 reachable as master" in await r.test()
        ov = await r.overview()
        node = ov["nodes"][0]
        assert node["memory"]["used_pct"] == 96.0 and node["stats"]["hit_ratio"] == 0.9
        assert node["replication"]["replicas"][0]["lag_bytes"] == 1000
        codes = {f["code"].split(":")[0]: f["severity"] for f in datastores.redis_findings("cache", ov)}
        assert codes["redis_memory"] == "bad" and codes["redis_rdb"] == "bad" and codes["redis_clients"] == "warn" and codes["redis_replica_lag"] == "warn"
        slow = await r.slowlog()
        assert slow[0]["command"].startswith("GET session:#") and "alice" not in str(slow)
        sample = await r.keyspace_sample(50)
        assert sample["top"][0]["key"].startswith("session:#") and "alice" not in str(sample) and sample["prefixes"][0]["prefix"] == "session"
        clients = await r.clients()
        assert clients["total"] == 2 and clients["blocked"] == 1
        await r.aclose()
        sent = {c[0].upper() for c in srv.commands}
        assert sent <= {"AUTH", "INFO", "CONFIG", "SLOWLOG", "CLIENT", "SCAN", "MEMORY"}
        assert ["AUTH", "moks", "pw"] in srv.commands


async def test_redis_refuses_writes_and_maps_errors(redis_cred, monkeypatch):
    async with FakeRedisServer(denied=("MEMORY",)) as srv:
        r = RedisConnector({"host": "127.0.0.1", "port": srv.port}, redis_cred)
        for cmd in (("SET", "a", "b"), ("CONFIG", "SET", "maxmemory", "1"), ("FLUSHALL",), ("CLIENT", "KILL", "ID", "1")):
            with pytest.raises(ConnectorError) as e:
                await r._call("x", *cmd)
            assert e.value.kind == "bad_request"
        assert not any(c[0].upper() in ("SET", "FLUSHALL") for c in srv.commands)
        sample = await r.keyspace_sample(20)
        assert sample["top"] == [] and "MEMORY USAGE is not permitted" in sample["note"]
        await r.aclose()
        monkeypatch.setenv("MOKS_CRED_REDIS_BAD", "nope")
        with pytest.raises(ConnectorError) as e:
            await RedisConnector({"host": "127.0.0.1", "port": srv.port}, {"type": "env", "name": "MOKS_CRED_REDIS_BAD"}).test()
        assert e.value.kind == "auth"
        with pytest.raises(ConnectorError) as e:
            await RedisConnector({"host": "127.0.0.1", "port": srv.port}).test()
        assert e.value.kind == "auth"
    with pytest.raises(ConnectorError) as e:
        await RedisConnector({"host": "127.0.0.1", "port": 1}).test()
    assert e.value.kind == "unavailable"
    with pytest.raises(ConnectorError):
        RedisConnector({"host": "x", "mode": "bogus"})


async def test_redis_cluster_mode_reports_failed_nodes_and_slots(redis_cred):
    async with FakeRedisServer(cluster=True) as srv:
        r = RedisConnector({"host": "127.0.0.1", "port": srv.port, "mode": "cluster"}, redis_cred)
        ov = await r.overview()
        assert ov["cluster"]["cluster_state"] == "fail" and len(ov["nodes"]) == 2
        assert ov["nodes"][1]["reachable"] is False
        codes = [f["code"] for f in datastores.redis_findings("c", ov)]
        assert any(c.startswith("redis_cluster_state") for c in codes) and any(c.startswith("redis_slots_fail") for c in codes)
        assert any(c.startswith("redis_unreachable") for c in codes)
        await r.aclose()


@pytest.mark.skipif(not shutil.which("redis-server"), reason="redis-server is not installed")
async def test_redis_against_a_real_server_with_a_minimal_acl(tmp_path, monkeypatch):
    port = 26000 + os.getpid() % 1000
    proc = subprocess.Popen(["redis-server", "--port", str(port), "--save", "", "--dir", str(tmp_path), "--slowlog-log-slower-than", "0"],
                            stdout=subprocess.DEVNULL)
    try:
        for _ in range(50):
            if subprocess.run(["redis-cli", "-p", str(port), "ping"], capture_output=True).returncode == 0:
                break
            await asyncio.sleep(0.1)
        acl = ["ACL", "SETUSER", "moks", "on", ">moks-pass", "+info", "+slowlog|get", "+latency|latest", "+client|list", "+memory|usage", "+scan",
               "+config|get", "+cluster|info", "~*"]
        subprocess.run(["redis-cli", "-p", str(port), *acl], check=True, capture_output=True)
        subprocess.run(["redis-cli", "-p", str(port), "set", "order:1", "x" * 3000], check=True, capture_output=True)
        monkeypatch.setenv("MOKS_CRED_REDIS_REAL", "moks-pass")
        r = RedisConnector({"host": "127.0.0.1", "port": port, "username": "moks"}, {"type": "env", "name": "MOKS_CRED_REDIS_REAL"})
        assert "reachable as master" in await r.test()
        ov = await r.overview()
        assert ov["nodes"][0]["reachable"] and ov["config"]["maxmemory-policy"]
        assert (await r.keyspace_sample(10))["top"][0]["key"].startswith("order:#")
        assert await r.slowlog(5)
        await r.aclose()
    finally:
        proc.terminate()
        proc.wait(5)


# ---- PostgreSQL (real server, pg_monitor role) ------------------------------------------------------

async def _admin():
    import asyncpg

    return await asyncpg.connect(ADMIN_URL)


@pytest.fixture
async def monitor_role(monkeypatch):
    role = f"moks_mon_{uuid.uuid4().hex[:6]}"
    conn = await _admin()
    await conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'mon-pass' IN ROLE pg_monitor")
    await conn.close()
    monkeypatch.setenv("MOKS_CRED_PG_TEST", "mon-pass")
    host, port = ADMIN_URL.split("@", 1)[1].split("/", 1)[0].split(":")
    yield {"host": host, "port": int(port), "database": "postgres", "username": role, "sslmode": "disable"}, {"type": "env", "name": "MOKS_CRED_PG_TEST"}
    conn = await _admin()
    await conn.execute(f"DROP ROLE IF EXISTS {role}")
    await conn.close()


async def test_postgres_views_as_pg_monitor(monitor_role):
    cfg, cred = monitor_role
    pg = PostgresConnector(cfg, cred)
    assert "reachable as primary" in await pg.test()
    ov = await pg.overview()
    assert ov["server"]["role"] == "primary" and ov["connections"]["max"] > 0 and ov["replication"]["role"] == "primary"
    assert isinstance(datastores.postgres_findings("db", ov), list)
    maint = await pg.maintenance()
    assert any(d["database"] == "postgres" for d in maint["databases"])
    assert {s["name"] for s in await pg.settings()} >= {"max_connections", "wal_level"}
    top = await pg.top_statements()
    assert "available" in top
    with pytest.raises(ConnectorError):
        await pg.top_statements(order="1; drop table x")
    # The session itself is read-only: even a statement outside the catalog cannot write.
    import asyncpg

    with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
        await pg._conn.execute("CREATE TABLE moks_should_not_exist (id int)")
    await pg.aclose()


async def test_postgres_lock_chains_and_long_transactions(monitor_role):
    cfg, cred = monitor_role
    admin = await _admin()
    table = f"moks_lock_{uuid.uuid4().hex[:6]}"
    await admin.execute(f"CREATE TABLE {table} (id int)")
    holder, waiter = await _admin(), await _admin()
    try:
        await holder.execute("BEGIN")
        await holder.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")
        wait_task = asyncio.create_task(waiter.execute(f"SELECT count(*) FROM {table} WHERE id = 12345"))
        pg = PostgresConnector(cfg, cred)
        for _ in range(50):
            locks = await pg.locks()
            if locks["blocked"]:
                break
            await asyncio.sleep(0.1)
        assert locks["root_blockers"] and locks["root_blockers"][0]["waiting"] >= 1
        assert locks["root_blockers"][0]["blocker"]["pid"] == holder.get_server_pid()
        assert "12345" not in str(locks["blocked"])  # literals are normalised away
        sessions = await pg.activity()
        assert any(s["pid"] == holder.get_server_pid() for s in sessions)
        ov = await pg.overview()
        assert ov["blocked_sessions"] >= 1
        assert any(f["code"].startswith("pg_blocked") for f in datastores.postgres_findings("db", ov))
        await pg.aclose()
    finally:
        await holder.execute("ROLLBACK")
        await asyncio.wait_for(wait_task, 10)
        for c in (holder, waiter):
            await c.close()
        await admin.execute(f"DROP TABLE {table}")
        await admin.close()


async def test_postgres_errors_are_actionable(monitor_role):
    cfg, _ = monitor_role
    with pytest.raises(ConnectorError) as e:
        await PostgresConnector(cfg, {"type": "env", "name": "MOKS_CRED_PG_WRONG"}).test()
    assert e.value.kind == "config"  # unset credential
    os.environ["MOKS_CRED_PG_WRONG"] = "wrong"
    try:
        with pytest.raises(ConnectorError) as e:
            await PostgresConnector(cfg, {"type": "env", "name": "MOKS_CRED_PG_WRONG"}).test()
        assert e.value.kind == "auth"
    finally:
        del os.environ["MOKS_CRED_PG_WRONG"]
    with pytest.raises(ConnectorError) as e:
        await PostgresConnector({**cfg, "port": 1}, None).test()
    assert e.value.kind == "unavailable"
    with pytest.raises(ConnectorError):
        PostgresConnector({**cfg, "sslmode": "sometimes"}, None)


def test_sql_normalisation_drops_literals():
    q = "SELECT * FROM users WHERE email = 'a@b.com' AND id IN (1, 2, 3) AND score > 4.5 AND note = $tag$secret$tag$ AND x = $1"
    out = normalize_sql(q)
    assert "a@b.com" not in out and "secret" not in out and "4.5" not in out and "(?, …)" in out and "$1" in out


# ---- findings ------------------------------------------------------------------------------------

def test_postgres_findings_thresholds():
    ov = {"connections": {"total": 95, "max": 100, "used_pct": 95.0}, "blocked_sessions": 6, "oldest_xid_age": 1_200_000_000, "cache_hit_ratio": 0.5,
          "long_transactions": [{"pid": 7, "xact_age_s": 4000, "state": "idle in transaction", "application": "billing"}],
          "replication": {"role": "primary", "replicas": [{"application_name": "r1", "replay_lag_s": 400, "replay_lag_bytes": 0, "state": "catchup"}],
                          "slots": [{"slot_name": "old", "active": False, "retained_bytes": 20 << 30, "wal_status": "extended"},
                                    {"slot_name": "gone", "active": False, "retained_bytes": None, "wal_status": "lost"}]}}
    found = {f["code"]: f["severity"] for f in datastores.postgres_findings("shop", ov)}
    assert found["pg_connections:shop"] == "bad" and found["pg_blocked:shop"] == "bad" and found["pg_xid_age:shop"] == "bad"
    assert found["pg_long_xact:shop"] == "bad" and found["pg_replica_lag:shop:r1"] == "bad" and found["pg_replica_state:shop:r1"] == "warn"
    assert found["pg_slot_retained:shop:old"] == "bad" and found["pg_slot_lost:shop:gone"] == "bad" and found["pg_cache_hit:shop"] == "info"
    quiet = {"connections": {"used_pct": 10}, "replication": {"role": "primary", "replicas": [], "slots": []}, "cache_hit_ratio": 0.99}
    assert datastores.postgres_findings("shop", quiet) == []
    t = datastores.thresholds({"thresholds": {"pg_connections_warn_pct": 5, "pg_connections_bad_pct": 50}})
    assert datastores.postgres_findings("shop", quiet, t)[0]["severity"] == "warn"


def test_redis_summary_and_key_labels():
    s = summarize_info(parse_info(INFO))
    assert s["memory"]["used_pct"] == 96.0 and s["clients"]["connected"] == 9000
    assert key_label("session:alice", False).startswith("session:#") and key_label("session:alice", True) == "session:alice"


# ---- scope and the tool executor -----------------------------------------------------------------

class FakePg:
    type = "postgres"
    calls: list[str] = []

    def __init__(self, config, credential_ref=None):
        self.config = config

    async def aclose(self):
        pass

    async def test(self):
        return "fake"

    async def overview(self):
        FakePg.calls.append(self.config["host"])
        return {"server": {"role": "primary"}, "connections": {"total": 99, "max": 100, "used_pct": 99.0}, "blocked_sessions": 0,
                "replication": {"role": "primary", "replicas": [], "slots": []}}

    async def settings(self):
        return []


@pytest.fixture
def fake_pg(monkeypatch):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "postgres", FakePg)
    registry._cache.clear()
    FakePg.calls = []
    yield
    registry._cache.clear()


async def _ctx(env_id, principal, patterns=("*",)):
    from moks.db import session_scope

    async with session_scope() as db:
        env = await db.get(Environment, env_id)
        connectors, _ = await registry.for_environment(db, env.id)
    return ToolContext(None, env, Scope(env.name, tuple(patterns), ("*",), ()), principal, NOW - timedelta(hours=1), NOW, connectors, timedelta(hours=6))


async def test_tools_enforce_the_cluster_namespace(fake_pg):
    name = f"ds-{uuid.uuid4().hex[:6]}"
    env_id = await make_env(name, connectors=[
        {"type": "postgres", "name": "shop-db", "config": {"host": "shop", "namespace": "shop"}},
        {"type": "postgres", "name": "pay-db", "config": {"host": "pay", "namespace": "payments"}},
        {"type": "postgres", "name": "rds", "config": {"host": "rds"}},
    ])
    shop_only = Principal("u", "u@x", [Binding("investigator", name, "shop")])
    ctx = await _ctx(env_id, shop_only, ("shop",))
    ex = ToolExecutor(None, ctx)
    assert {"datastore_clusters", "pg_overview", "pg_locks"} <= {t.name for t in tools_for_planner(ctx)}
    assert not {"redis_overview"} & {t.name for t in tools_for_planner(ctx)}  # no redis cluster configured

    ok = await ex.run("pg_overview", {"cluster": "shop-db"})
    assert ok.ok and any(f["code"] == "pg_connections:shop-db" for f in ok.data["findings"])
    denied = await ex.run("pg_overview", {"cluster": "pay-db"})
    assert denied.denied and "payments" in denied.error
    no_ns = await ex.run("pg_overview", {"cluster": "rds"})
    assert no_ns.denied and "all namespaces" in no_ns.error
    missing = await ex.run("pg_overview", {"cluster": "nope"})
    assert not missing.ok and missing.error_kind == "not_found" and "shop-db" in missing.error and "pay-db" not in missing.error
    assert FakePg.calls == ["shop"]  # nothing was read from the clusters the caller may not see

    listed = await ex.run("datastore_clusters", {})
    assert [c["name"] for c in listed.data] == ["shop-db"] and listed.data[0]["findings"]
    assert (await ex.run("pg_overview", {"cluster": "Bad Name"})).error_kind == "invalid_args"


def test_scope_helper_without_namespace_needs_cluster_wide_access():
    class Row:
        name, type, config = "rds", "postgres", {}

    wide = Principal("u", "u@x", [Binding("investigator")])
    datastores.require(Row(), Scope("e", ("*",), ("*",), ()), wide, "e")
    with pytest.raises(ScopeError):
        datastores.require(Row(), Scope("e", ("shop",), ("*",), ()), Principal("u", "u@x", [Binding("investigator", "e", "shop")]), "e")

    class Denied:
        name, type, config = "sys", "redis", {"namespace": "kube-system"}

    with pytest.raises(ScopeError):
        datastores.require(Denied(), Scope("e", ("*",), ("*",), ("kube-system",)), wide, "e")


def test_tools_are_registered_read_only():
    for name in ("datastore_clusters", "pg_overview", "pg_sessions", "pg_locks", "pg_replication", "pg_maintenance", "pg_top_queries",
                 "redis_overview", "redis_slowlog", "redis_clients", "redis_keyspace"):
        spec = REGISTRY[name]
        assert spec.read_only and not spec.privileged


# ---- discovery and the admin API -------------------------------------------------------------------

class DiscoveryK8s:
    async def cnpg_list(self, plural):
        return [{"metadata": {"name": "shop-db", "namespace": "shop"}, "spec": {"instances": 3, "bootstrap": {"initdb": {"database": "orders"}}}},
                {"metadata": {"name": "sys-db", "namespace": "kube-system"}, "spec": {}}]

    async def list_services_all(self):
        svc = lambda ns, name, port, labels=None: {"metadata": {"name": name, "namespace": ns, "labels": labels or {}},  # noqa: E731
                                                   "spec": {"ports": [{"port": port}], "selector": {}, "clusterIP": "10.0.0.1"}}
        return [svc("shop", "shop-db-rw", 5432), svc("cache", "redis-master", 6379, {"app.kubernetes.io/name": "redis"}),
                svc("cache", "redis-sentinel", 26379, {"app.kubernetes.io/name": "redis"}), svc("web", "nginx", 80),
                svc("legacy", "postgres", 5432)]


async def test_discovery_proposes_clusters_without_reading_secrets():
    found = await datastores.discover(DiscoveryK8s(), Scope("e", ("*",), ("*",), ("kube-system",)))
    pg = {c["name"]: c for c in found["postgres"]}
    assert set(pg) == {"shop-db", "postgres"}  # kube-system is denied; the -rw service is not listed twice
    assert pg["shop-db"]["config"] == {"host": "shop-db-rw.shop.svc", "port": 5432, "database": "orders", "sslmode": "require",
                                       "namespace": "shop", "service": "shop-db-rw", "username": "moks_monitor"}
    redis = {c["name"]: c["config"]["mode"] for c in found["redis"]}
    assert redis == {"redis-master": "standalone", "redis-sentinel": "sentinel"}


async def test_admin_accepts_datastore_connectors(client):
    h = await login(client, "admin@example.com")
    name = f"ds-admin-{uuid.uuid4().hex[:6]}"
    await make_env(name)
    r = await client.post("/moks/api/admin/connectors", headers=h, json={"environment": name, "type": "redis", "name": "cache",
                                                                          "config": {"host": "redis.cache.svc", "namespace": "cache", "mode": "standalone"},
                                                                          "credential_ref": {"type": "env", "name": "MOKS_CRED_REDIS_CACHE"}})
    assert r.status_code == 201, r.text
    r = await client.post("/moks/api/admin/connectors", headers=h, json={"environment": name, "type": "postgres", "name": "db",
                                                                          "config": {"host": "db", "password": "x"}})
    assert r.status_code == 422
    r = await client.get(f"/moks/api/admin/datastores/discover?environment={name}", headers=h)
    assert r.status_code == 409  # no kubernetes connector
