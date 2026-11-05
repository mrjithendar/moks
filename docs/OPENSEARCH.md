# OpenSearch for MOKS knowledge

PostgreSQL is the source of truth for documents, access, ingestion state and indexing events. OpenSearch holds a derived copy, and every result is checked against PostgreSQL before anyone sees it. Losing OpenSearch loses speed and vector search, not knowledge.

**This is persistence, not high availability.** One pod, on one node, on a disk it shares with everything else on mk4. The four minikube nodes are containers on one Hetzner host: they are not four failure domains. If that host or disk fails, the volume goes with it, and a PVC is not a backup.

## Two clusters during the move

mk4 already runs OpenSearch as its live backend: `moks-opensearch`, version 3.9.0, with the security plugin disabled, plain HTTP, no probes, and 1,942 chunks at 3072 dimensions (measured 2026-11-02). The hardened cluster, `moks-search`, is built beside it rather than over it, because it has never run on mk4 and a failed first start must not touch search.

| | `moks-opensearch` (original) | `moks-search` (hardened) |
| --- | --- | --- |
| Security plugin, login | off, none | on, five identities |
| TLS | none | HTTP and transport, private CA, verified by MOKS |
| Probes | none | startup, readiness, liveness |
| Mapping | version 1, dynamic | version 2, strict |
| Network | every `app=moks` pod | API, worker, ops Job, securityadmin Job, monitoring namespace |
| Generations in PostgreSQL | backend `opensearch` | backend `opensearch-secure` |

Each cluster has its own active generation, so moving between them is a configuration change that leaves the other's record intact. `rag.opensearch.secure.enabled` deploys the cluster and moves nothing. `rag.opensearch.secure.active` moves search and indexing.

Both live in the Argo CD Application `moks-opensearch`, the one owner. With `secure.enabled: false`, the mk4 render is byte-identical to before this work.

## Capacity

| Measured | Result |
| --- | --- |
| mk4 nodes | arm64, kernel 6.12, 4 KB pages, k8s 1.37. Each reports 16 CPU and 31 GiB, but all four are one host, so these are not additive |
| Requests on the busiest node (mk4-m02) | 840m CPU, 3346 Mi memory, before this cluster |
| Disk | **`/dev/sda1` 151 G, 96% used, 6.5 G free at the last check (falling through the day)**, shared by all nodes. This is the tightest constraint |
| `vm.max_map_count` | 1048576 on the nodes, above the 262144 OpenSearch asks for, so no privileged init container or host change is needed. Its persistence across host reboots is not verified; `allowMmap: false` is the fallback |
| Live pod | 1487 Mi of 2 Gi limit, 254 MiB of 1 GiB heap used, 49.6 MB index |
| StorageClass | `k8s.io/minikube-hostpath`: reclaim Delete, no expansion, no node affinity, ignores `fsGroup`, no quota |

Local benchmark, real OpenSearch 3.9.0 on an arm64 Mac (not mk4, and synthetic text and random vectors, so treat as order of magnitude), secured cluster, 1 GiB heap, 2,000 chunks at 3072 dimensions: indexing 23.5 s, keyword p50/p95 4.0/7.4 ms, filtered k-NN p50/p95 13.5/14.9 ms, store 30 MB, heap 264 MiB, process 1128 MiB.

Provisional allocation: heap 1g, requests 250m and 2Gi, limit 2 CPU and 2Gi, `storage: 10Gi`. The figures above do not support raising any of these. Re-measure on mk4 under real traffic before changing them. During the move two clusters run, which adds about 2 GiB memory and a little disk. The 10Gi is a label: hostPath enforces nothing, and it cannot be expanded.

Mitigations for hostPath: `nodeName` pins the pod to the node holding its data, `fixVolumePermissions` runs a one-time chown as root with only CHOWN and FOWNER (not privileged), and watermarks are in bytes (`low: 3gb, high: 2gb, floodStage: 1gb`) because percentages would already be past their limits on this disk. Moving the data to another node means a snapshot restore or a rebuild.

## What was built

- **Deployment.** `templates/opensearch-secure.yaml`: StatefulSet, ClusterIP Service, NetworkPolicy, config from `files/opensearch/`, a PostSync `securityadmin` Job, and a suspended CronJob that `scripts/opensearch-migrate.sh` runs one step at a time. The image is pinned by digest.
- **Identities.** `moks_search` (API, reads `moks-chunks-*`), `moks_indexer` (worker, writes documents, cannot create or delete an index), `moks_lifecycle` (worker and ops Job, index generations and snapshots), `moks_monitor` (numbers, no document text), `moks_admin` (people). An API pod receives only the search password, and only `ca.crt`, never the node key.
- **Mapping version 2.** Strict, with keyword fields for identifiers and metadata, English analysis for prose, a `.exact` field that keeps names such as `moks-api`, `MOKS_RAG_BACKEND` and `deploy/helm/values.yaml` whole, offsets, freshness, and the embedding model. Lifecycle, trust and classification describe a chunk as of its last write; they never decide access.
- **Retrieval.** BM25, exact identifiers and filtered k-NN, fused client-side with the existing RRF. No search pipeline or extra plugin. The PostgreSQL identifier ranking stays. The optional reranker works unchanged and is not required.
- **Authorization.** Whole-binding grants before ranking. Sources and documents taken out of search are read from PostgreSQL per search and excluded in the query, so a quarantine works at commit. Past 1,000 ids, or for a character-class grant, the filter is incomplete, `filtered_before_ranking` says so, and bounded expansion applies. PostgreSQL's check runs on every result either way. Stale-content policy is applied only in that final check.
- **Consistency.** The existing outbox, plus: writes fenced by content revision so an older write cannot replace a newer one; every bulk item read back; chunks written before old ones are pruned, so a failure leaves old text rather than none; an embedding outage never replaces good vectors with nothing; `sync_generation` brings a generation level with PostgreSQL from `document_index_state` alone.
- **Lifecycle.** Retired generations are kept 168 hours (`MOKS_RAG_RETIRED_RETENTION_HOURS`) and can be activated again. Age never touches an active index. Before a build, MOKS refuses a red cluster, a write block, or too little disk. `drop_index` refuses any name outside `moks-chunks-`.
- **Fallback.** If OpenSearch fails, keyword search goes to PostgreSQL within the 3 s search timeout, this process stops asking for 30 s, and `fallback_reason` says what happened. A missing index is a fault now, where it used to return nothing.
- **Monitoring.** The worker probes each minute, records the result for the admin page (API pods cannot ask), and exports `moks_search_backend{metric=...}`. Alerts and dashboard panels are in `deploy/grafana`. The exporter plugin adds JVM and index detail.
- **Admin.** Knowledge > Index health > Search backend, for holders of `admin.llm`: backend, generation, connection health, counts, model and dimensions, retrieval modes and fallbacks, backfill, generations, last snapshot.

Application logs stay in Loki. Knowledge indexes are never mixed with operational logs.

## Setup, in order

All of this is yours to run: it writes to mk4 and needs a push, because Argo CD reads `origin/master`. Commands assume `kubectl --context mk4`. Stop at any failure; nothing below step 6 affects search.

### 1. Ship the code

The `moks` image must contain migration 0030 and this code before anything else. Push, wait for CI, then `kubectl -n moks rollout restart deploy/moks-api deploy/moks-worker`. Search stays on the original cluster, now with the new write path. Confirm: `curl -fsS https://jithendar.com/moks/api/health`.

Take stock of what you will compare against, from the worker:

```sh
kubectl --context mk4 -n moks exec deploy/moks-worker -- moks opensearch status
```

### 2. Free disk

At 6.5 GB free, the 1 GB flood-stage block is close, and it stops writes for every generation. A build refuses to start without room, but other workloads share this disk.

### 3. Create the Secrets

```sh
scripts/opensearch-secrets.sh mk4
```

Refuses if any exists. Needs `openssl` and `htpasswd`. The CA key is discarded on purpose.

### 4. Deploy beside the original

In `values-minikube.yaml`, set `rag.opensearch.secure.enabled: true`. Push.

```sh
argocd app sync moks-foundation     # lets MOKS pods reach moks-search; never --prune
argocd app sync moks-opensearch     # creates moks-search and runs the security Job
kubectl --context mk4 -n moks rollout status statefulset/moks-search --timeout=300s
kubectl --context mk4 -n moks wait --for=condition=Complete job/moks-search-security --timeout=300s
kubectl --context mk4 patch pv "$(kubectl --context mk4 -n moks get pvc data-moks-search-0 -o jsonpath='{.spec.volumeName}')" \
  -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'
scripts/opensearch-migrate.sh status
```

`status` should report `live: false` and `health: green`. Then prove NetworkPolicy is enforced here, which has never been shown on mk4's kindnet. This must time out or fail:

```sh
kubectl --context mk4 -n moks run np-probe --rm -it --restart=Never --image=curlimages/curl -- \
  curl -sk -m 5 -o /dev/null -w '%{http_code}\n' https://moks-search:9200/
```

### 5. Build and check, with search untouched

```sh
scripts/opensearch-migrate.sh build      # re-embeds all chunks, which costs provider calls and budget
scripts/opensearch-migrate.sh validate   # exits 1 unless every check passes
scripts/opensearch-migrate.sh compare
```

Vectors are generated again rather than copied. `validate` compares mapping, counts, vectors and coverage with PostgreSQL, finds real identifiers, runs a vector search, checks an unknown scope returns nothing private, and proves the search identity cannot write. `compare` is read-only, returns overlap counts and no text, and stops at 50 queries. Lower overlap against the original is expected, because prose is now stemmed. Investigate `new_empty_where_old_answered` above 0.

If it fails, fix the cause and build again: the failed generation is cleaned up, and search never moved.

### 6. Cut over

The original's `publicRoute` and `dashboards` both reach the unauthenticated pod and cannot follow, so the chart refuses to cut over until they are off. Decide what replaces them first. Then set in `values-minikube.yaml`:

```yaml
rag:
  opensearch:
    publicRoute: {enabled: false}
    dashboards: {enabled: false}
    secure: {enabled: true, active: true}   # keep the existing keys in this block
```

Push, and remove the Middlewares and IngressRoutes by hand, because syncs here never prune.

```sh
scripts/opensearch-migrate.sh sync       # as close to the switch as possible
argocd app sync moks-foundation moks-opensearch
argocd app sync moks-api moks-worker
scripts/opensearch-migrate.sh sync       # what changed during the switch, and adopts vector state
scripts/opensearch-migrate.sh validate
```

Pods restart with the new configuration; a search during the rollout uses whichever configuration its pod has, and both are valid. Check Knowledge > Index health shows `opensearch-secure`, TLS-verified, and a green probe, and run a real search.

### 7. Soak, then retire the original

Keep the original for at least the retention window. Before retiring it, run `scripts/opensearch-migrate.sh validate` once more against the active cluster and run the same real searches you ran at cutover. Only when both pass, record it: `secure.cutoverVerified: "<active generation> validated <YYYY-MM-DD>"`. The chart refuses `retireOriginal` without that line, because a cluster being active does not show it was verified. Then set `secure.retireOriginal: true`, push, sync `moks-foundation` and `moks-opensearch`, and delete the StatefulSet, Service and NetworkPolicy by hand. Its PVC stays until the data deletion below.

## Rollback

**Before step 6:** set `secure.enabled: false` if you want it gone. Search never moved.

**After step 6:**

1. Set `secure.active: false`, push, then `argocd app sync moks-foundation moks-opensearch moks-api moks-worker`.
2. Catch the original up: `kubectl --context mk4 -n moks exec deploy/moks-worker -- moks opensearch sync`.

Nothing is lost. The original stopped receiving events at cutover, but PostgreSQL holds every update and delete, and `document_index_state` shows which documents that generation missed. `sync` rewrites those and removes deleted ones. The worker also does this slowly by itself, 50 documents every 600 ticks. After rollback the original is unauthenticated again, and `publicRoute` and `dashboards` can return.

**To an earlier generation in the same cluster,** within retention:

```sh
scripts/opensearch-migrate.sh activate moks-chunks-xxxxxxxx
```

This refuses a generation built with another embedding model. Rollback to Pinecone or PostgreSQL needs a `rag.backend` change and a `moks reindex`.

## OpenSearch Dashboards for administrators

**Engineers do not need this.** The Knowledge Explorer is the way to browse knowledge, under MOKS access control. Dashboards is for administrators looking at the cluster itself.

> **Dashboards does not inherit MOKS RBAC.** OpenSearch knows nothing of MOKS bindings. Anyone who signs in sees every chunk their OpenSearch role allows, across all environments and namespaces, including quarantined and archived documents. Give these logins only to people cleared for all knowledge.

Optional and off by default: `rag.opensearch.secure.dashboards`. It needs `moks-search`, and never touches the original cluster or the `rag.opensearch.dashboards` block that serves it.

| | |
| --- | --- |
| Reach | ClusterIP. Port-forward first; ingress is optional |
| Server identity | `moks_dashboards`, mapped to the built-in `kibana_server`. It never reaches a browser |
| People | `moks_viewer`: reads `moks-chunks-*` without the `embedding` field, read-only mode. `moks_admin` still exists for administration |
| TLS | `verificationMode: full` against the private CA. The pod mounts `ca.crt` and no key |
| Anonymous access | Refused: the security plugin has `anonymous_auth_enabled: false` |
| Network | May reach `moks-search:9200` and DNS. Nothing may reach it except through a port-forward, or from the ingress namespaces if ingress is on |
| Image | Pinned by digest, on the server's version |

To turn it on, with `moks-search` running:

1. If `moks-search-users` already exists from before this was added, it lacks these identities. Delete all the Secrets and rerun `scripts/opensearch-secrets.sh`, which issues new passwords for everything. On a fresh install, step 3 above already made them.
2. Set `rag.opensearch.secure.dashboards.enabled: true` in `values-minikube.yaml`, push, then `argocd app sync moks-foundation moks-opensearch`. The security Job loads the new role and users.
3. Connect and sign in as `moks_viewer`:

```sh
kubectl --context mk4 -n moks get secret moks-search-viewer-login -o jsonpath='{.data.password}' | base64 -d; echo
kubectl --context mk4 -n moks port-forward svc/moks-search-dashboards 5601
# open http://localhost:5601 and create the index pattern moks-chunks-*
```

**Ingress is optional.** `dashboards.ingress.enabled: true` serves it at `https://<host>/opensearch` with `server.basePath`, `rewriteBasePath` and a secure cookie, and then sync `moks-routing` too. The path is what the read-only API route uses today, so the chart refuses both at once: turn `publicRoute` off, or choose another `ingress.path`. Everything there still needs an OpenSearch login, and it is reachable from the internet, so port-forward is the better default.

Not yet run: this deployment has been rendered and its settings tested, but Dashboards has not started against a real `moks-search`, and neither the `${VAR}` password references nor the base path have been exercised by a running server. Check both on first use.

## Backup and restore

| Lost | Recoverable from | Needs |
| --- | --- | --- |
| Documents, chunks, access, ingestion state, outbox | PostgreSQL backups, off-cluster | Their own restore ([OPERATOR_GUIDE.md](OPERATOR_GUIDE.md#backup-and-restore)) |
| Index contents | PostgreSQL alone: `opensearch-migrate.sh build` | For hybrid search, the same embedding model, its API key and budget. Without them, keyword only, and MOKS says so |
| Index contents, faster, with vectors | An OpenSearch snapshot, then `sync` for what changed after it | The snapshot repository, its credentials |
| Security configuration | Git and `scripts/opensearch-secrets.sh` | New passwords and CA |
| OpenSearch audit events | Pod logs only, while retained | Nothing else holds them |

Snapshots need a custom image, because upstream does not ship `repository-s3`:

1. Apply the ECR repository, the same way as earlier targeted applies: `aws_ecr_repository.opensearch` and the CI role change in `deploy/terraform`.
2. Push. CI job `opensearch-image` reports a digest.
3. Create an R2 bucket and a token limited to it, and Secret `moks-search-snapshots` (`deploy/secrets/moks-search-snapshots.example.yaml`). Do not reuse the `MOKS_CRED_R2_*` keys.
4. Set `secure.image` to `881026659929.dkr.ecr.us-east-1.amazonaws.com/moks-opensearch@sha256:...`, `pluginsInstalled: true`, `snapshots: {enabled: true, endpoint: https://<account>.r2.cloudflarestorage.com}`. Push, sync.
5. `scripts/opensearch-migrate.sh snapshots --bucket <name>` registers the repository, which makes the node prove it can write there, and a daily schedule keeping 14 snapshots for at most 30 days. Only `moks-chunks-*` is included.

R2 has never been tested with OpenSearch's S3 client, whose recent checksum behaviour can differ from what S3-compatible stores accept. If registration fails, use real S3 or rely on rebuilds; snapshots are an accelerator here, not the only copy.

The last successful snapshot appears on the admin page, and `MoksSnapshotOverdue` alerts after 36 hours.

**Restore drill.** Into a new name, so nothing serving is touched, as `moks_admin`:

```sh
kubectl --context mk4 -n moks port-forward svc/moks-search 9200 &
PASS="$(kubectl --context mk4 -n moks get secret moks-search-admin-login -o jsonpath='{.data.password}' | base64 -d)"
kubectl --context mk4 -n moks get secret moks-search-tls -o jsonpath='{.data.ca\.crt}' | base64 -d > /tmp/moks-ca.crt
curl -u "moks_admin:$PASS" --cacert /tmp/moks-ca.crt --resolve moks-search:9200:127.0.0.1 \
  -XPOST 'https://moks-search:9200/_snapshot/moks-knowledge/<snapshot>/_restore?wait_for_completion=true' -H 'content-type: application/json' \
  -d '{"indices":"moks-chunks-*","rename_pattern":"(.+)","rename_replacement":"$1-drill","include_global_state":false}'
```

Compare `_count` with the original, then delete the `-drill` index as `moks_admin`.

## Deleting data

Nothing does this automatically. Sync prune is never used, the volume claim is annotated against Argo CD deletion, and the StatefulSet retains claims when deleted or scaled. To delete, deliberately:

```sh
kubectl --context mk4 -n moks delete pvc data-moks-opensearch-0
kubectl --context mk4 get pv | grep moks-opensearch   # Delete reclaim removes it; a Retain volume needs kubectl delete pv
```

An unused index is deleted as `moks_admin`, never by age. Before deleting anything, confirm which generation `active` is on the admin page.

## Operations

**Rotating certificates.** Certificates last 1,095 days. The CA key is not kept, so rotation replaces everything: delete the five Secrets, rerun `scripts/opensearch-secrets.sh`, sync `moks-opensearch`, restart `moks-search`, the security Job, and the API and worker. Search falls back to PostgreSQL meanwhile.

**Changing roles or users.** Edit `files/opensearch/`, push, sync `moks-opensearch`. The Job loads them. Files read at first start are not read again.

**Changing the mapping.** Raise `MAPPING_VERSION`, change `index_body`, then build a new generation. An index mapping is never edited in place.

**Upgrading OpenSearch.** The image, the exporter URL and the plugin version move together, because a plugin must match the server exactly. Snapshot or be ready to rebuild first.

**Prometheus.** Add the `moks` job from `deploy/grafana/prometheus-scrape.yaml` in the tools repository; without it, the worker's gauges go unread. The `moks-search` job also needs a Secret with the monitor password and CA.

## Troubleshooting

| Symptom | Cause and action |
| --- | --- |
| `moks-search-0` Pending | Wrong `nodeName`, or the volume's node. hostPath does not follow the pod |
| CrashLoop, `AccessDenied` on data | `fixVolumePermissions` is off, or the init container did not run |
| CrashLoop on TLS | A Secret is missing, or the key is not PKCS#8. Rerun the script after deleting all five |
| Readiness stays failing, security `SERVICE_UNAVAILABLE` | First start could not load the config files. Check the pod log, then the security Job |
| `auth` in `fallback_reason` | A password in `moks-search-clients` differs from the hash loaded. Regenerate both |
| `fallback_reason` with `missing` or `unavailable`, Index health healthy | The active generation's index is gone: `activate` a retained one, or build |
| Index writes 403, `cluster_block_exception` | Flood-stage watermark. Free disk; the block lifts on its own |
| Outbox growing | Search still works from the last index state, and PostgreSQL filters results. Fix the cluster; events retry eight times, then go dead. Retry from the Knowledge page |
| Vector coverage below 100% | Embedding failed. Documents stay searchable by keyword, and backfill retries each minute, stopping at the first failure |
| Validate `coverage` fails | Run `sync`. If it stays, read `first` in the report |
| Health yellow | A system index asked for a replica. `cluster.default_number_of_replicas: 0` prevents new ones; set replicas to 0 on any that predate it |
