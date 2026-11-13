"""Redis / Valkey connector: read-only SRE views of one standalone, Sentinel-managed or clustered deployment.

Configuration (non-secret JSON) and a credential reference holding the password:

    {"mode": "standalone", "host": "redis-master.cache.svc", "port": 6379, "username": "moks",
     "namespace": "cache", "service": "redis-master", "tls": false, "timeout_seconds": 10}
    {"mode": "sentinel", "host": "redis.cache.svc", "port": 26379, "sentinel_master": "mymaster", ...}
    {"mode": "cluster",  "host": "redis-cluster.cache.svc", "port": 6379, ...}

Read-only by construction: the class sends only the commands in READ_COMMANDS, and the recommended ACL
(docs/DATASTORES.md) grants nothing else. Key *values* are never read. Key names leave the connector hashed
unless config.show_key_names is true, because names often embed user IDs or emails.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

from ..observability import CONNECTOR_LATENCY, CONNECTOR_REQUESTS
from .base import ConnectorError, resolve_credential
from .resp import RespConnection, RespError, parse_client_list, parse_cluster_nodes, parse_info, raise_for

READ_COMMANDS = frozenset({"AUTH", "PING", "INFO", "SLOWLOG", "LATENCY", "CLIENT", "SCAN", "MEMORY", "TYPE", "CLUSTER", "ROLE", "SENTINEL",
                           "CONFIG", "DBSIZE", "SELECT"})
# Subcommands, where the command alone would also allow writes.
READ_SUBCOMMANDS = {"SLOWLOG": {"GET", "LEN"}, "LATENCY": {"LATEST", "DOCTOR"}, "CLIENT": {"LIST", "INFO"}, "MEMORY": {"USAGE", "STATS"},
                    "CLUSTER": {"INFO", "NODES"}, "SENTINEL": {"GET-MASTER-ADDR-BY-NAME", "MASTERS", "REPLICAS"}, "CONFIG": {"GET"}}
CONFIG_KEYS = ("maxmemory", "maxmemory-policy", "maxclients", "appendonly", "save", "timeout")
MAX_CLUSTER_NODES = 12
SUBCOMMAND_COMMANDS = frozenset({"CONFIG", "CLIENT", "CLUSTER", "MEMORY", "SLOWLOG", "LATENCY", "XINFO", "OBJECT", "ACL", "SCRIPT", "FUNCTION",
                                 "COMMAND", "MODULE", "DEBUG", "INFO", "XGROUP", "PUBSUB", "SENTINEL"})
MODES = ("standalone", "sentinel", "cluster")


def key_label(key: str, show: bool) -> str:
    """`session:8f3…` keeps the prefix that names the data set; the rest is hashed unless names may be shown."""
    if show:
        return key[:200]
    prefix, sep, _ = key.partition(":")
    digest = hashlib.sha256(key.encode()).hexdigest()[:10]
    return f"{prefix[:40]}:#{digest}" if sep else f"#{digest}"


class RedisConnector:
    type = "redis"

    def __init__(self, config: dict[str, Any], credential_ref: dict[str, Any] | None = None):
        self.mode = config.get("mode", "standalone")
        if self.mode not in MODES:
            raise ConnectorError("config", f"redis mode must be one of {', '.join(MODES)}")
        self.host = str(config.get("host") or "")
        if not self.host:
            raise ConnectorError("config", "redis connector needs config.host (a Service DNS name or address)")
        self.port = int(config.get("port") or (26379 if self.mode == "sentinel" else 6379))
        self.username = config.get("username") or None
        self.sentinel_master = config.get("sentinel_master") or "mymaster"
        # Sentinels are often unauthenticated, or protected by their own password: AUTH only when asked.
        self.sentinel_auth = bool(config.get("sentinel_auth", False))
        self.tls = bool(config.get("tls", False))
        self.verify_tls = bool(config.get("verify_tls", True))
        self.timeout = float(config.get("timeout_seconds", 10))
        self.show_key_names = bool(config.get("show_key_names", False))
        self.namespace = config.get("namespace")
        self.credential_ref = credential_ref
        self._password: str | None = resolve_credential(credential_ref)
        self._conns: dict[tuple[str, int], RespConnection] = {}
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        for c in list(self._conns.values()):
            await c.close()
        self._conns.clear()

    # ---- plumbing --------------------------------------------------------------------------

    async def _conn(self, host: str, port: int, auth: bool = True) -> RespConnection:
        conn = self._conns.get((host, port))
        if conn is not None and conn.connected:
            return conn
        conn = RespConnection(host, port, timeout=self.timeout, tls=self.tls, verify_tls=self.verify_tls)
        await conn.connect()
        if self._password is not None and auth:
            args = ("AUTH", self.username, self._password) if self.username else ("AUTH", self._password)
            reply = await conn.call(*args)
            if isinstance(reply, RespError):
                await conn.close()
                raise_for(reply, "AUTH")
        self._conns[(host, port)] = conn
        return conn

    async def _call(self, operation: str, *parts: str | int, node: tuple[str, int] | None = None) -> Any:
        cmd = str(parts[0]).upper()
        if cmd not in READ_COMMANDS or (cmd in READ_SUBCOMMANDS and len(parts) > 1 and str(parts[1]).upper() not in READ_SUBCOMMANDS[cmd]):
            raise ConnectorError("bad_request", f"{' '.join(map(str, parts[:2]))} is not a read command MOKS may send")
        start = time.monotonic()
        host, port = node or await self._primary()
        last: ConnectorError | None = None
        for attempt in range(2):  # every command here is a read: one retry on a dropped connection
            try:
                async with self._lock:
                    sentinel = self.mode == "sentinel" and (host, port) == (self.host, self.port)
                    conn = await self._conn(host, port, auth=not sentinel or self.sentinel_auth)
                    reply = await conn.call(*parts)
                reply = raise_for(reply, " ".join(map(str, parts[:2])))
                CONNECTOR_REQUESTS.labels("redis", operation, "ok").inc()
                CONNECTOR_LATENCY.labels("redis", operation).observe(time.monotonic() - start)
                return reply
            except ConnectorError as exc:
                last = exc
                if not exc.retryable or attempt == 1:
                    break
                await asyncio.sleep(0.3)
        assert last is not None
        CONNECTOR_REQUESTS.labels("redis", operation, last.kind).inc()
        raise last

    async def _primary(self) -> tuple[str, int]:
        if self.mode != "sentinel":
            return self.host, self.port
        reply = await self._call("sentinel", "SENTINEL", "get-master-addr-by-name", self.sentinel_master, node=(self.host, self.port))
        if not reply or len(reply) != 2:
            raise ConnectorError("not_found", f"sentinel knows no master named {self.sentinel_master!r}")
        return str(reply[0]), int(reply[1])

    async def nodes(self) -> list[dict[str, Any]]:
        """The data nodes this deployment has: one for standalone and Sentinel, every reachable node for a cluster."""
        if self.mode != "cluster":
            host, port = await self._primary()
            return [{"host": host, "port": port, "role": "primary"}]
        text = await self._call("cluster_nodes", "CLUSTER", "NODES")
        rows = parse_cluster_nodes(text)
        for r in rows:
            if not r["host"] or r["port"] is None:  # a node with no announced address: reach it through the seed
                r["host"], r["port"] = (self.host, self.port) if "myself" in r["flags"] else (r["host"], r["port"])
        return [r for r in rows if r["host"] and r["port"]][:MAX_CLUSTER_NODES]

    async def info(self, node: tuple[str, int] | None = None) -> dict[str, dict[str, Any]]:
        return parse_info(await self._call("info", "INFO", node=node))

    async def config(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in CONFIG_KEYS:
            try:
                reply = await self._call("config_get", "CONFIG", "GET", key)
            except ConnectorError as exc:
                if exc.kind in ("forbidden", "bad_request"):  # managed services (ElastiCache) refuse CONFIG; INFO still has maxmemory
                    return out
                raise
            if isinstance(reply, list) and len(reply) >= 2:
                out[reply[0]] = reply[1]
        return out

    # ---- views -------------------------------------------------------------------------------

    async def test(self) -> str:
        info = await self.info()
        server = info.get("server", {})
        what = f"{'Valkey' if server.get('valkey_version') else 'Redis'} {server.get('valkey_version') or server.get('redis_version', '?')}"
        role = info.get("replication", {}).get("role", "?")
        extra = ""
        if self.mode == "cluster":
            ci = parse_info(await self._call("cluster_info", "CLUSTER", "INFO")).get("default", {})
            extra = f", cluster_state {ci.get('cluster_state')}, {ci.get('cluster_known_nodes')} nodes"
        elif self.mode == "sentinel":
            host, port = await self._primary()
            extra = f", sentinel master {self.sentinel_master} at {host}:{port}"
        return f"{what} reachable as {role}{extra}"

    async def overview(self) -> dict[str, Any]:
        """INFO summary per node plus config: the inputs to every Redis finding."""
        out: dict[str, Any] = {"mode": self.mode, "nodes": [], "partial": False}
        nodes = await self.nodes()
        cluster_info: dict[str, Any] = {}
        if self.mode == "cluster":
            cluster_info = parse_info(await self._call("cluster_info", "CLUSTER", "INFO")).get("default", {})
        for n in nodes:
            if n.get("failed"):
                out["nodes"].append({**_node_id(n), "reachable": False, "error": "node flagged as failed by the cluster"})
                continue
            target = None if self.mode != "cluster" else (n["host"], n["port"])
            try:
                info = await self.info(target)
            except ConnectorError as exc:
                out["partial"] = True
                out["nodes"].append({**_node_id(n), "reachable": False, "error": f"{exc.kind}: {exc.message}"})
                continue
            out["nodes"].append({**_node_id(n), "reachable": True, **summarize_info(info)})
        out["cluster"] = {k: cluster_info.get(k) for k in ("cluster_state", "cluster_slots_assigned", "cluster_slots_ok", "cluster_slots_pfail",
                                                           "cluster_slots_fail", "cluster_known_nodes", "cluster_size")} if cluster_info else None
        out["config"] = await self.config()
        return out

    async def slowlog(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = await self._call("slowlog", "SLOWLOG", "GET", max(1, min(limit, 128)))
        out = []
        for r in rows or []:
            if not isinstance(r, list) or len(r) < 4:
                continue
            args = r[3] if isinstance(r[3], list) else []
            out.append({"id": r[0], "at": r[1], "duration_us": r[2], "command": _command_shape(args, self.show_key_names),
                        "client": r[4] if len(r) > 4 else None, "client_name": r[5] if len(r) > 5 else None})
        return out

    async def latency(self) -> list[dict[str, Any]]:
        rows = await self._call("latency", "LATENCY", "LATEST")
        return [{"event": r[0], "at": r[1], "latest_ms": r[2], "max_ms": r[3]} for r in rows or [] if isinstance(r, list) and len(r) >= 4]

    async def clients(self, limit: int = 200) -> dict[str, Any]:
        rows = parse_client_list(await self._call("client_list", "CLIENT", "LIST"))
        by_name: dict[str, int] = {}
        blocked = [r for r in rows if "b" in str(r.get("flags", ""))]
        for r in rows:
            label = r.get("name") or r.get("addr", "?").rsplit(":", 1)[0]
            by_name[label] = by_name.get(label, 0) + 1
        keep = sorted(rows, key=lambda r: (-int(r.get("idle", 0) or 0)))[:limit]
        return {
            "total": len(rows), "blocked": len(blocked),
            "by_source": sorted(({"source": k, "connections": v} for k, v in by_name.items()), key=lambda x: -x["connections"])[:30],
            "longest_idle": [{k: r.get(k) for k in ("id", "addr", "name", "age", "idle", "flags", "db", "cmd", "tot-mem", "omem")} for r in keep[:20]],
            "largest_output_buffers": [{k: r.get(k) for k in ("id", "addr", "name", "omem", "cmd")}
                                       for r in sorted(rows, key=lambda r: -int(r.get("omem", 0) or 0))[:10] if int(r.get("omem", 0) or 0) > 0],
        }

    async def keyspace_sample(self, sample: int = 500, top: int = 15) -> dict[str, Any]:
        """Big keys from a bounded SCAN sample (never KEYS, never values). Approximate by design."""
        sample = max(10, min(sample, 2000))
        seen: list[str] = []
        cursor = "0"
        for _ in range(20):
            reply = await self._call("scan", "SCAN", cursor, "COUNT", 200)
            cursor, keys = str(reply[0]), reply[1] or []
            seen.extend(keys)
            if cursor == "0" or len(seen) >= sample:
                break
        seen = seen[:sample]
        sized = []
        prefixes: dict[str, list[int]] = {}
        for key in seen:
            try:
                size = await self._call("memory_usage", "MEMORY", "USAGE", key, "SAMPLES", 5)
            except ConnectorError as exc:
                if exc.kind in ("forbidden", "bad_request"):
                    return {"sampled": len(seen), "note": f"MEMORY USAGE is not permitted ({exc.message}); grant +memory|usage", "top": [], "prefixes": []}
                raise
            if size is None:
                continue
            sized.append((int(size), key))
            prefix = key.partition(":")[0] if ":" in key else "(no prefix)"
            prefixes.setdefault(prefix[:40], []).append(int(size))
        sized.sort(reverse=True)
        return {
            "sampled": len(seen), "complete_scan": cursor == "0", "names_hashed": not self.show_key_names,
            "top": [{"key": key_label(k, self.show_key_names), "bytes": s} for s, k in sized[:top]],
            "prefixes": sorted(({"prefix": p, "keys": len(v), "bytes": sum(v), "largest": max(v)} for p, v in prefixes.items()), key=lambda x: -x["bytes"])[:20],
            "note": "Sample of the keyspace, not a full scan: sizes are approximate (MEMORY USAGE with 5 samples).",
        }


def _node_id(n: dict[str, Any]) -> dict[str, Any]:
    return {"node": f"{n.get('host')}:{n.get('port')}", "role": n.get("role")}


def _command_shape(args: list[Any], show_keys: bool) -> str:
    """The command and its first key, never the values it carried."""
    if not args:
        return "?"
    cmd = str(args[0]).upper()[:40]
    if cmd in SUBCOMMAND_COMMANDS and len(args) > 1:  # CONFIG GET, CLIENT LIST: the second word is not a key
        return f"{cmd} {str(args[1]).upper()[:40]}" + (f" (+{len(args) - 2} args)" if len(args) > 2 else "")
    if len(args) > 1:
        return f"{cmd} {key_label(str(args[1]), show_keys)}" + (f" (+{len(args) - 2} args)" if len(args) > 2 else "")
    return cmd


def _num(d: dict[str, Any], key: str, default: float = 0) -> float:
    v = d.get(key, default)
    return float(v) if isinstance(v, (int, float)) else default


def summarize_info(info: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The INFO fields an SRE reads first, with derived ratios. Pure: the detectors use it too."""
    server, clients, memory = info.get("server", {}), info.get("clients", {}), info.get("memory", {})
    stats, persistence, replication = info.get("stats", {}), info.get("persistence", {}), info.get("replication", {})
    used, maxmem = _num(memory, "used_memory"), _num(memory, "maxmemory")
    hits, misses = _num(stats, "keyspace_hits"), _num(stats, "keyspace_misses")
    replicas = []
    for k, v in replication.items():
        if k.startswith("slave") and k[5:].isdigit() and isinstance(v, dict):
            replicas.append({"addr": f"{v.get('ip')}:{v.get('port')}", "state": v.get("state"), "offset": v.get("offset"), "lag_s": v.get("lag"),
                             "lag_bytes": max(0, int(_num(replication, "master_repl_offset")) - int(v.get("offset") or 0))})
    keyspace = {db: v for db, v in (info.get("keyspace") or {}).items() if isinstance(v, dict)}
    return {
        "version": server.get("valkey_version") or server.get("redis_version"),
        "uptime_s": server.get("uptime_in_seconds"),
        "role": replication.get("role"),
        "memory": {"used_bytes": int(used), "max_bytes": int(maxmem), "used_pct": round(100 * used / maxmem, 1) if maxmem else None,
                   "rss_bytes": int(_num(memory, "used_memory_rss")), "fragmentation_ratio": memory.get("mem_fragmentation_ratio"),
                   "policy": memory.get("maxmemory_policy")},
        "clients": {"connected": clients.get("connected_clients"), "blocked": clients.get("blocked_clients"),
                    "maxclients": clients.get("maxclients"), "rejected_total": stats.get("rejected_connections")},
        "stats": {"ops_per_sec": stats.get("instantaneous_ops_per_sec"), "evicted_keys_total": stats.get("evicted_keys"),
                  "expired_keys_total": stats.get("expired_keys"), "hit_ratio": round(hits / (hits + misses), 4) if hits + misses else None},
        "persistence": {"loading": persistence.get("loading"), "rdb_last_bgsave_status": persistence.get("rdb_last_bgsave_status"),
                        "rdb_last_save_time": persistence.get("rdb_last_save_time"), "rdb_changes_since_last_save": persistence.get("rdb_changes_since_last_save"),
                        "aof_enabled": persistence.get("aof_enabled"), "aof_last_write_status": persistence.get("aof_last_write_status"),
                        "aof_last_bgrewrite_status": persistence.get("aof_last_bgrewrite_status")},
        "replication": {"role": replication.get("role"), "connected_replicas": replication.get("connected_slaves"), "replicas": replicas,
                        "master_link_status": replication.get("master_link_status"), "master_last_io_seconds_ago": replication.get("master_last_io_seconds_ago"),
                        "master_sync_in_progress": replication.get("master_sync_in_progress")},
        "keyspace": keyspace,
    }
