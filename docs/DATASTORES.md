# PostgreSQL and Redis clusters

MOKS can watch the databases and caches your workloads depend on. Each one is a connector of type `postgres`
or `redis` in an environment. An environment may hold any number of each, and tools and pages address them by
connector name (`shop-db`, `cache`).

## What MOKS reads

| Cluster | Views | Tools |
|---|---|---|
| PostgreSQL 13+ (CloudNativePG, Bitnami, RDS/Aurora, plain) | role (primary/standby), connections by state and client, long and idle-in-transaction sessions, lock chains with root blockers, replication lag and slots with retained WAL, standby receiver state, transaction-ID age, dead rows and (auto)vacuum, cache hit ratio, key settings, `pg_stat_statements` | `pg_overview`, `pg_sessions`, `pg_locks`, `pg_replication`, `pg_maintenance`, `pg_top_queries` |
| Redis / Valkey (standalone, Sentinel, cluster) | per-node memory against `maxmemory`, fragmentation, clients, hit ratio, evictions, RDB/AOF status, replication links and lag, cluster slot state and failed nodes, slowlog, latency events, client list, big keys from a sample | `redis_overview`, `redis_slowlog`, `redis_clients`, `redis_keyspace` |

`datastore_clusters` lists the clusters a person may read, with reachability and findings, and gives the
investigation the names the other tools take.

Findings (`moks/datastores.py`) use the same severities as the Backups page. Each threshold can be changed per
connector under `config.thresholds`, for example `{"pg_connections_warn_pct": 70}`. The defaults are listed in
`DEFAULTS` in that file.

## Read-only by construction

- **PostgreSQL.** Use a role that holds only `pg_monitor`. Every session also runs with
  `default_transaction_read_only=on`, a 5 s `statement_timeout`, a 500 ms `lock_timeout` and
  `idle_in_transaction_session_timeout`. The connector sends only the statements in `QUERIES`
  (`moks/connectors/postgres.py`), and none of them takes SQL from a caller. ORDER BY and LIMIT come from
  allowlists and bounds.
- **Redis.** The connector refuses any command outside its read list before sending it. The ACL below grants
  nothing else, so a bug cannot write either.
- **Query text.** Query text from `pg_stat_activity` is normalised, so literals (emails, tokens, IDs) become `?`.
  Set `query_text` to `none` to drop query text, or `raw` to show it as is. Every result also goes through
  MOKS's redaction.
- **Key values and names.** Redis key *values* are never read. Key names are shown as `prefix:#hash`
  unless `show_key_names` is true. The slowlog shows the command and its first key only.
- **Credentials.** Passwords are credential references (`MOKS_CRED_*` or a mounted file), like every other
  connector. Discovery reads Services and CloudNativePG Clusters, never Secrets.

## Who sees a cluster

Set `config.namespace` to the namespace the cluster runs in. A person can read the cluster only when they hold
`explorer.read` or `investigation.run` there and the environment allows that namespace. A cluster with no
namespace (RDS, ElastiCache, anything outside Kubernetes) needs access to all namespaces in the environment.
MOKS checks this before it connects, so a denied cluster is never contacted. The denial is audited and
recorded like any other tool denial.

## Setting up PostgreSQL

Create a monitoring role on each cluster:

```sql
CREATE ROLE moks_monitor LOGIN PASSWORD '…' IN ROLE pg_monitor;
-- optional, for pg_top_queries (needs shared_preload_libraries = 'pg_stat_statements')
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
```

With CloudNativePG, declare it in the Cluster so the operator keeps it:

```yaml
spec:
  managed:
    roles:
      - name: moks_monitor
        login: true
        inRoles: [pg_monitor]
        passwordSecret: {name: moks-monitor-shop-db}   # a basic-auth Secret with username and password
  postgresql:
    shared_preload_libraries: [pg_stat_statements]
```

Then put the same password into `moks-secrets` under a `MOKS_CRED_` key, and add the connector:

```json
{"host": "shop-db-rw.shop.svc", "port": 5432, "database": "app", "username": "moks_monitor",
 "sslmode": "require", "namespace": "shop", "service": "shop-db-rw"}
```

Point at the primary's service (`-rw` on CloudNativePG) to see replicas, slots and write activity. A
connector on a replica service reports standby state instead.

## Setting up Redis or Valkey

Create an ACL user. On Redis 7 and later, and on Valkey:

```
ACL SETUSER moks on >… resetkeys %R~* resetchannels -@all +ping +info +config|get +slowlog|get +latency|latest
  +client|list +scan +memory|usage +cluster|info +cluster|nodes
```

On Redis 6, use `~*` in place of `%R~*`. Put the ACL in the chart's `users.acl` or `aclfile`, so it
survives a restart.

```json
{"host": "redis-master.cache.svc", "port": 6379, "mode": "standalone", "username": "moks", "namespace": "cache"}
{"host": "redis.cache.svc", "port": 26379, "mode": "sentinel", "sentinel_master": "mymaster", "username": "moks", "namespace": "cache"}
{"host": "redis-cluster.cache.svc", "port": 6379, "mode": "cluster", "username": "moks", "namespace": "cache"}
```

- **Sentinel.** MOKS asks the Sentinel for the current primary and reads that. It authenticates to the
  Sentinel only with `"sentinel_auth": true`, using the same credential.
- **Cluster.** MOKS reads `CLUSTER NODES` from the seed and `INFO` from up to 12 nodes. It reports nodes that
  fail or that it cannot reach.
- **Managed services.** ElastiCache and other managed services refuse `CONFIG GET`. MOKS then takes
  `maxmemory` from `INFO`.
- **TLS.** Set `"tls": true`. Use `"verify_tls": false` only when node certificates do not name the address MOKS
  connects to.

## Network access

The chart's NetworkPolicy blocks egress by default. Allow each namespace and port:

```yaml
networkPolicy:
  inCluster:
    - {namespace: shop, ports: [5432]}
    - {namespace: cache, ports: [6379, 26379]}
```

## Discovery

**Administration → Connectors → Discover databases** lists the CloudNativePG clusters and the PostgreSQL and
Redis Services in the environment's allowed namespaces. **Add** fills in the form with the suggested settings.
You still create the role or ACL user and the credential yourself.
