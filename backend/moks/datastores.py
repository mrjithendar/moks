"""Data-store clusters (PostgreSQL and Redis/Valkey connectors): scope, discovery and deterministic findings.

A data-store cluster is a connector row of type `postgres` or `redis`. Unlike the other connector types an
environment may hold several of each; tools and pages address them by connector name.

Scope: config.namespace names the Kubernetes namespace the cluster belongs to, and the caller needs read
access there (and the environment must allow it). A cluster with no namespace (RDS, ElastiCache, something
outside Kubernetes) needs environment-wide access, like other cluster-scoped data.

Findings are pure functions over the connector's overview, so the tools, the Data page and the detectors
(PR 2) agree on what is a problem. Severity follows the Backups page: bad, warn, info.
"""
from __future__ import annotations

from typing import Any

from .connectors.base import ConnectorError
from .rbac import P
from .scope import ScopeError

KINDS = ("postgres", "redis")
READ_PERMS = (P.EXPLORER_READ, P.INVESTIGATION_RUN)

# Thresholds. A connector may override any of them under config.thresholds.
DEFAULTS: dict[str, float] = {
    "pg_connections_warn_pct": 80, "pg_connections_bad_pct": 90,
    "pg_long_xact_warn_s": 300, "pg_long_xact_bad_s": 1800,
    "pg_blocked_warn": 1, "pg_blocked_bad": 5,
    "pg_xid_age_warn": 500_000_000, "pg_xid_age_bad": 1_000_000_000,
    "pg_replay_lag_warn_s": 60, "pg_replay_lag_bad_s": 300,
    "pg_slot_retained_warn_bytes": 1 << 30, "pg_slot_retained_bad_bytes": 10 << 30,
    "pg_cache_hit_warn": 0.90,
    "redis_memory_warn_pct": 85, "redis_memory_bad_pct": 95,
    "redis_clients_warn_pct": 80, "redis_clients_bad_pct": 95,
    "redis_fragmentation_warn": 1.5, "redis_fragmentation_min_rss_bytes": 100 << 20,
    "redis_replica_lag_warn_s": 10,
}


def thresholds(config: dict[str, Any] | None) -> dict[str, float]:
    over = (config or {}).get("thresholds") or {}
    return {k: float(over[k]) if isinstance(over.get(k), (int, float)) else v for k, v in DEFAULTS.items()}


# ---- scope ----------------------------------------------------------------------------------------


def cluster_namespace(row: Any) -> str | None:
    ns = (row.config or {}).get("namespace")
    return str(ns) if ns else None


def visible(row: Any, scope: Any, principal: Any, environment: str, perms: tuple[P, ...] = READ_PERMS) -> bool:
    try:
        require(row, scope, principal, environment, perms)
    except ScopeError:
        return False
    return True


def require(row: Any, scope: Any, principal: Any, environment: str, perms: tuple[P, ...] = READ_PERMS) -> None:
    ns = cluster_namespace(row)
    if ns is None:
        scope.require_cluster_wide(f"data-store cluster {row.name!r} (no namespace configured)")
        if not any(principal.can(p, environment, "*") for p in perms):
            raise ScopeError(f"data-store cluster {row.name!r} needs read access to all namespaces in {environment!r}")
        return
    if not any(principal.can(p, environment, ns) for p in perms):
        raise ScopeError(f"you have no read access to namespace {ns!r} (data-store cluster {row.name!r}) in {environment!r}")
    scope.require(ns)


def describe(row: Any) -> dict[str, Any]:
    """What may be shown about a cluster row: never the credential reference itself."""
    cfg = row.config or {}
    return {"name": row.name, "kind": row.type, "namespace": cfg.get("namespace"), "service": cfg.get("service"),
            "host": cfg.get("host"), "port": cfg.get("port"), "mode": cfg.get("mode") if row.type == "redis" else None,
            "database": cfg.get("database") if row.type == "postgres" else None,
            "last_test_ok": row.last_test_ok, "last_test_at": row.last_test_at.isoformat() if row.last_test_at else None}


async def in_scope(db: Any, env: Any, scope: Any, principal: Any, kind: str | None = None) -> tuple[list[tuple[Any, Any]], dict[str, str]]:
    """Enabled data-store clusters of this environment the caller may read, built, with per-name build errors."""
    from .connectors import registry

    out: list[tuple[Any, Any]] = []
    errors: dict[str, str] = {}
    for k in (kind,) if kind else KINDS:
        rows, errs = await registry.of_type(db, env.id, k)
        out.extend((r, inst) for r, inst in rows if visible(r, scope, principal, env.name))
        errors.update(errs)
    return out, errors


async def resolve(db: Any, env: Any, scope: Any, principal: Any, kind: str, name: str) -> tuple[Any, Any]:
    """One cluster by connector name, after the scope check. Unknown and out-of-scope look different on purpose:
    an operator needs to know whether to fix the name or ask for access, and names are not secret."""
    from .connectors import registry

    rows, errors = await registry.of_type(db, env.id, kind)
    for row, inst in rows:
        if row.name == name:
            require(row, scope, principal, env.name)
            return row, inst
    if name in errors:
        raise ConnectorError("config", f"{kind} cluster {name!r} is misconfigured: {errors[name]}")
    known = sorted(r.name for r, _ in rows if visible(r, scope, principal, env.name))
    raise ConnectorError("not_found", f"no enabled {kind} cluster named {name!r} in {env.name!r}" + (f"; known: {', '.join(known)}" if known else ""))


# ---- findings -----------------------------------------------------------------------------------


def _add(out: list, severity: str, code: str, text: str) -> None:
    out.append({"severity": severity, "code": code, "text": text})


def _gib(n: float) -> str:
    return f"{n / (1 << 30):.1f} GiB"


def postgres_findings(name: str, ov: dict[str, Any], t: dict[str, float] | None = None) -> list[dict[str, str]]:
    t = t or DEFAULTS
    out: list[dict[str, str]] = []
    conn = ov.get("connections") or {}
    pct = conn.get("used_pct") or 0
    if pct >= t["pg_connections_bad_pct"]:
        _add(out, "bad", f"pg_connections:{name}", f"{name}: {conn.get('total')} of {conn.get('max')} connections in use ({pct}%). New clients will soon be refused.")
    elif pct >= t["pg_connections_warn_pct"]:
        _add(out, "warn", f"pg_connections:{name}", f"{name}: {conn.get('total')} of {conn.get('max')} connections in use ({pct}%).")
    longest = max((s.get("xact_age_s") or 0 for s in ov.get("long_transactions") or []), default=0)
    if longest >= t["pg_long_xact_warn_s"]:
        s = max(ov["long_transactions"], key=lambda r: r.get("xact_age_s") or 0)
        _add(out, "bad" if longest >= t["pg_long_xact_bad_s"] else "warn", f"pg_long_xact:{name}",
             f"{name}: a transaction has been open for {longest / 60:.0f} min (pid {s.get('pid')}, {s.get('application') or s.get('user')}, state {s.get('state')}). "
             "It holds back vacuum and may hold locks.")
    blocked = ov.get("blocked_sessions") or 0
    if blocked >= t["pg_blocked_warn"]:
        _add(out, "bad" if blocked >= t["pg_blocked_bad"] else "warn", f"pg_blocked:{name}", f"{name}: {blocked} session(s) are waiting on locks held by other sessions.")
    xid = ov.get("oldest_xid_age") or 0
    if xid >= t["pg_xid_age_warn"]:
        _add(out, "bad" if xid >= t["pg_xid_age_bad"] else "warn", f"pg_xid_age:{name}",
             f"{name}: oldest database transaction ID age is {xid:,}. At about 2.1 billion PostgreSQL stops accepting writes; vacuum must freeze it first.")
    hit = ov.get("cache_hit_ratio")
    if hit is not None and hit < t["pg_cache_hit_warn"]:
        _add(out, "info", f"pg_cache_hit:{name}", f"{name}: buffer cache hit ratio since the last stats reset is {hit:.1%}; reads are going to disk.")
    rep = ov.get("replication") or {}
    for r in rep.get("replicas") or []:
        lag = r.get("replay_lag_s") or 0
        if lag >= t["pg_replay_lag_warn_s"]:
            _add(out, "bad" if lag >= t["pg_replay_lag_bad_s"] else "warn", f"pg_replica_lag:{name}:{r.get('application_name')}",
                 f"{name}: replica {r.get('application_name') or r.get('client')} replays {lag:.0f}s behind the primary ({_gib(r.get('replay_lag_bytes') or 0)}).")
        if r.get("state") not in (None, "streaming"):
            _add(out, "warn", f"pg_replica_state:{name}:{r.get('application_name')}", f"{name}: replica {r.get('application_name') or r.get('client')} is {r.get('state')}, not streaming.")
    for s in rep.get("slots") or []:
        kept = s.get("retained_bytes") or 0
        if s.get("wal_status") == "lost":
            _add(out, "bad", f"pg_slot_lost:{name}:{s.get('slot_name')}", f"{name}: replication slot {s.get('slot_name')} has lost required WAL; its consumer must be rebuilt.")
        elif not s.get("active") and kept >= t["pg_slot_retained_warn_bytes"]:
            _add(out, "bad" if kept >= t["pg_slot_retained_bad_bytes"] else "warn", f"pg_slot_retained:{name}:{s.get('slot_name')}",
                 f"{name}: inactive replication slot {s.get('slot_name')} retains {_gib(kept)} of WAL. The disk fills until it is consumed or dropped.")
    standby = rep.get("standby")
    if standby:
        if standby.get("receiver_status") not in ("streaming",):
            _add(out, "bad", f"pg_standby_receiver:{name}", f"{name}: this standby's WAL receiver is {standby.get('receiver_status') or 'not running'}; it is not receiving changes.")
        delay = standby.get("replay_delay_s") or 0
        if delay >= t["pg_replay_lag_bad_s"]:
            _add(out, "warn", f"pg_standby_delay:{name}", f"{name}: last replayed transaction is {delay / 60:.0f} min old. On an idle primary this is expected; otherwise the standby is behind.")
    return out


def redis_findings(name: str, ov: dict[str, Any], t: dict[str, float] | None = None) -> list[dict[str, str]]:
    t = t or DEFAULTS
    out: list[dict[str, str]] = []
    cluster = ov.get("cluster") or {}
    if cluster and cluster.get("cluster_state") not in (None, "ok"):
        _add(out, "bad", f"redis_cluster_state:{name}", f"{name}: cluster_state is {cluster.get('cluster_state')}; the cluster refuses queries for uncovered slots.")
    if cluster and (cluster.get("cluster_slots_fail") or 0) > 0:
        _add(out, "bad", f"redis_slots_fail:{name}", f"{name}: {cluster['cluster_slots_fail']} hash slots are in FAIL state.")
    elif cluster and (cluster.get("cluster_slots_pfail") or 0) > 0:
        _add(out, "warn", f"redis_slots_pfail:{name}", f"{name}: {cluster['cluster_slots_pfail']} hash slots are possibly failing (PFAIL).")
    for n in ov.get("nodes") or []:
        who = f"{name} {n.get('node')}" if len(ov.get("nodes") or []) > 1 else name
        code = f"{name}:{n.get('node')}"
        if not n.get("reachable"):
            _add(out, "bad", f"redis_unreachable:{code}", f"{who} is not reachable: {n.get('error')}.")
            continue
        mem = n.get("memory") or {}
        pct = mem.get("used_pct")
        if pct is not None and pct >= t["redis_memory_warn_pct"]:
            evicting = (mem.get("policy") or "noeviction") != "noeviction"
            _add(out, "bad" if pct >= t["redis_memory_bad_pct"] else "warn", f"redis_memory:{code}",
                 f"{who}: {pct}% of maxmemory used (policy {mem.get('policy')}). " + ("Keys are being evicted." if evicting and pct >= 99 else
                                                                                    "Writes will fail with OOM at the limit." if not evicting else ""))
        if not mem.get("max_bytes") and n.get("role") in ("master", "primary"):
            _add(out, "info", f"redis_no_maxmemory:{code}", f"{who}: maxmemory is not set, so Redis grows until the container's memory limit kills it.")
        frag = mem.get("fragmentation_ratio")
        if isinstance(frag, (int, float)) and frag >= t["redis_fragmentation_warn"] and (mem.get("rss_bytes") or 0) >= t["redis_fragmentation_min_rss_bytes"]:
            _add(out, "warn", f"redis_fragmentation:{code}", f"{who}: memory fragmentation ratio {frag} (RSS {_gib(mem['rss_bytes'])}); consider activedefrag.")
        if isinstance(frag, (int, float)) and 0 < frag < 1 and (mem.get("used_bytes") or 0) > 0:
            _add(out, "warn", f"redis_swap:{code}", f"{who}: fragmentation ratio {frag} is below 1: part of the dataset may be swapped out.")
        cl = n.get("clients") or {}
        if cl.get("maxclients") and cl.get("connected") is not None:
            cpct = 100 * cl["connected"] / cl["maxclients"]
            if cpct >= t["redis_clients_warn_pct"]:
                _add(out, "bad" if cpct >= t["redis_clients_bad_pct"] else "warn", f"redis_clients:{code}", f"{who}: {cl['connected']} of {cl['maxclients']} client connections in use.")
        p = n.get("persistence") or {}
        if p.get("rdb_last_bgsave_status") not in (None, "ok"):
            _add(out, "bad", f"redis_rdb:{code}", f"{who}: the last RDB snapshot failed; with stop-writes-on-bgsave-error (the default) writes are refused.")
        if p.get("aof_enabled") and p.get("aof_last_write_status") not in (None, "ok"):
            _add(out, "bad", f"redis_aof:{code}", f"{who}: the last AOF write failed.")
        if p.get("loading"):
            _add(out, "warn", f"redis_loading:{code}", f"{who} is loading its dataset and answers LOADING to most commands.")
        rep = n.get("replication") or {}
        if rep.get("role") == "slave" and rep.get("master_link_status") not in (None, "up"):
            _add(out, "bad", f"redis_master_link:{code}", f"{who}: replica's link to its primary is {rep.get('master_link_status')}.")
        for r in rep.get("replicas") or []:
            if r.get("state") not in (None, "online"):
                _add(out, "warn", f"redis_replica_state:{code}:{r.get('addr')}", f"{who}: replica {r.get('addr')} is {r.get('state')}.")
            elif (r.get("lag_s") or 0) >= t["redis_replica_lag_warn_s"]:
                _add(out, "warn", f"redis_replica_lag:{code}:{r.get('addr')}", f"{who}: replica {r.get('addr')} last acknowledged {r.get('lag_s')}s ago.")
    return out


async def overview_with_findings(row: Any, inst: Any) -> dict[str, Any]:
    ov = await inst.overview()
    fn = postgres_findings if row.type == "postgres" else redis_findings
    return {**ov, "findings": fn(row.name, ov, thresholds(row.config))}


# ---- discovery ----------------------------------------------------------------------------------

REDIS_IMAGES = ("redis", "valkey", "keydb")
REDIS_PORTS = {6379: "standalone", 26379: "sentinel"}
POSTGRES_PORT = 5432


async def discover(k8s: Any, scope: Any) -> dict[str, Any]:
    """Candidate clusters from what runs in Kubernetes, for the "add cluster" form. Reads Services and CloudNativePG
    Clusters only; credentials are never read, so every candidate still needs a monitoring role and a credential."""
    out: dict[str, Any] = {"postgres": [], "redis": [], "notes": []}
    cnpg_services: set[tuple[str, str]] = set()
    try:
        clusters = await k8s.cnpg_list("clusters")
    except ConnectorError as exc:
        clusters = []
        if exc.kind not in ("not_found", "forbidden"):
            out["notes"].append(f"CloudNativePG clusters could not be listed: {exc.message}")
    for c in clusters:
        meta, spec = c.get("metadata") or {}, c.get("spec") or {}
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        if not scope.allows(ns):
            continue
        cnpg_services.update({(ns, f"{name}-rw"), (ns, f"{name}-ro"), (ns, f"{name}-r")})
        db = ((spec.get("bootstrap") or {}).get("initdb") or {}).get("database") or "app"
        out["postgres"].append({"name": _slug(f"{name}"), "source": "cloudnative-pg", "namespace": ns, "service": f"{name}-rw",
                                "config": {"host": f"{name}-rw.{ns}.svc", "port": POSTGRES_PORT, "database": db, "sslmode": "require",
                                           "namespace": ns, "service": f"{name}-rw", "username": "moks_monitor"},
                                "instances": spec.get("instances"), "note": "CloudNativePG serves TLS by default. Create the moks_monitor role (docs/DATASTORES.md)."})
    try:
        services = await k8s.list_services_all()
    except ConnectorError as exc:
        out["notes"].append(f"Services could not be listed: {exc.message}")
        services = []
    for s in services:
        meta, spec = s.get("metadata") or {}, s.get("spec") or {}
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        if not scope.allows(ns) or (ns, name) in cnpg_services or spec.get("clusterIP") == "None" and name.endswith("-headless"):
            continue
        labels = {**(meta.get("labels") or {}), **(spec.get("selector") or {})}
        app = " ".join(str(labels.get(k, "")) for k in ("app.kubernetes.io/name", "app", "app.kubernetes.io/component")).lower()
        ports = {p.get("port") for p in spec.get("ports") or []}
        if POSTGRES_PORT in ports and ("postgres" in app or "postgres" in name):
            out["postgres"].append({"name": _slug(name), "source": "service", "namespace": ns, "service": name,
                                    "config": {"host": f"{name}.{ns}.svc", "port": POSTGRES_PORT, "database": "postgres", "sslmode": "prefer",
                                               "namespace": ns, "service": name, "username": "moks_monitor"},
                                    "note": "Create the moks_monitor role (docs/DATASTORES.md)."})
            continue
        hit = next((p for p in REDIS_PORTS if p in ports), None)
        if hit and (any(i in app for i in REDIS_IMAGES) or any(i in name for i in REDIS_IMAGES)):
            mode = REDIS_PORTS[hit]
            if "cluster" in name or "cluster" in app:
                mode = "cluster"
            out["redis"].append({"name": _slug(name), "source": "service", "namespace": ns, "service": name,
                                 "config": {"host": f"{name}.{ns}.svc", "port": hit, "mode": mode, "namespace": ns, "service": name, "username": "moks",
                                            **({"sentinel_master": "mymaster"} if mode == "sentinel" else {})},
                                 "note": "Create the moks ACL user (docs/DATASTORES.md)."})
    return out


def _slug(name: str) -> str:
    s = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in name.lower()).strip("-")
    return (s or "cluster")[:100]
