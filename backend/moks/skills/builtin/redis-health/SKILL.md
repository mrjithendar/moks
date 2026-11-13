---
name: redis-health
title: Redis health
version: '1'
owner: sre
category: application
description: Find why Redis or Valkey is slow, evicting, refusing writes or connections, or has lost a node or replica.
triggers:
  keywords:
  - redis
  - valkey
  - cache
  - evicted
  - eviction
  - maxmemory
  - OOM command not allowed
  - slowlog
  - sentinel
  - cluster_state
  findings: []
steps:
- id: clusters
  tool: datastore_clusters
  args:
    kind: redis
  purpose: Redis clusters in scope with their findings
  required: true
- tool: redis_keyspace
  args:
    cluster: '{cluster}'
    sample: 500
  for_each: finding.redis_memory
  limit: 2
  purpose: which keys and prefixes hold the memory
- tool: redis_clients
  args:
    cluster: '{cluster}'
  for_each: finding.redis_clients
  limit: 2
  purpose: who holds the connections
- tool: redis_slowlog
  args:
    cluster: '{cluster}'
  for_each: finding.redis_memory
  limit: 1
  purpose: slow commands and latency events
- tool: redis_overview
  args:
    cluster: '{cluster}'
  for_each: finding.redis_unreachable
  limit: 2
  purpose: per-node state and cluster slots
- tool: redis_overview
  args:
    cluster: '{cluster}'
  for_each: finding.redis_master_link
  limit: 2
  purpose: replication links
resource_types:
- Redis
hypotheses:
- statement: The dataset outgrew maxmemory.
  supports: Memory at the limit; with noeviction, writes fail with OOM; otherwise evictions rise.
  refutes: Memory well below the limit.
- statement: A few large keys or one prefix dominate.
  supports: The sampled top keys or one prefix hold most of the bytes.
  refutes: Sizes spread evenly.
- statement: Slow commands block the event loop.
  supports: Slowlog entries in the window; KEYS, large range reads or Lua scripts.
  refutes: Empty slowlog and no latency events.
- statement: Persistence or replication failed.
  supports: rdb_last_bgsave_status err, AOF write errors, a replica link down or a failed cluster node.
  refutes: Persistence ok and replicas online.
recovery_validation:
- datastore_clusters reports the Redis cluster reachable with memory and clients below the warning thresholds
detection:
- id: redis-memory-pressure
  kind: datastore
  finding: redis_memory
  sustained_checks: 2
  severity: sev2
  title: Redis memory near maxmemory
- id: redis-persistence-failing
  kind: datastore
  finding: redis_rdb
  sustained_checks: 1
  severity: sev2
  title: Redis snapshot failing
- id: redis-node-unreachable
  kind: datastore
  finding: redis_unreachable
  sustained_checks: 2
  severity: sev2
  title: Redis node unreachable
- id: redis-cluster-degraded
  kind: datastore
  finding: redis_cluster_state
  sustained_checks: 1
  severity: sev1
  title: Redis cluster not ok
- id: redis-replica-link-down
  kind: datastore
  finding: redis_master_link
  sustained_checks: 2
  severity: sev3
  title: Redis replica lost its primary
- id: redis-clients-saturated
  kind: datastore
  finding: redis_clients
  sustained_checks: 2
  severity: sev3
  title: Redis connections near maxclients
---
# Redis health

1. **Memory.** Compare `used_memory` with `maxmemory` and name the policy. With `noeviction`, a full Redis refuses writes (`OOM command not allowed`). Otherwise it evicts keys, and the hit ratio falls.
2. **Big keys.** Name the prefix that holds the bytes, not the individual keys: key names are hashed and values are never read.
3. **Slowlog.** `KEYS`, big `HGETALL`/`SMEMBERS`/`LRANGE` and Lua scripts block every client. Name the command shape.
4. **Persistence and replication.** A failed RDB save with `stop-writes-on-bgsave-error` refuses writes: check disk space and memory for the fork. On a cluster, name failed nodes and whether every slot is covered.

Ending client connections needs an approved action plan.
