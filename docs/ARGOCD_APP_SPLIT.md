# Splitting MOKS into separate Argo CD Applications

MOKS used to be one Argo CD Application per cluster, so one sync touched everything. It is now a root Application per cluster and one child per component, which can be inspected, synced and rolled back alone. All of them use the same chart with `releaseName: moks`. Each child sets `resourceGroup` to render only its own resources.

This page is the migration runbook and the reference for running the split afterwards. Nothing here has been run on a cluster. [Validation](#validation) separates what has been proved from what only a live run can prove.

## Applications

The roots run in `argocd` on mk4 and only write Application objects. Sync everything by hand, and never with prune.

| Group | mk4 (project `moks`) | EKS (project `moks-eks`, cluster `roboshop-demo`) | Holds |
|---|---|---|---|
| Root | `moks-root` | `roboshop-demo-moks-root` | The child Application objects, in project `moks-root` |
| foundation | `moks-foundation` | `roboshop-demo-moks-foundation` | Namespace metadata, shared ConfigMaps, shared ServiceAccounts and RBAC, `moks-metrics`, the `moks` NetworkPolicy |
| database | `moks-database` | `roboshop-demo-moks-database` | PostgreSQL StatefulSet, Service, NetworkPolicy |
| api | `moks-api` | `roboshop-demo-moks-api` | API Deployment, Service, PodDisruptionBudget. The frontend ships in this image |
| worker | `moks-worker` | `roboshop-demo-moks-worker` | Worker Deployments, including `worker.pools` |
| executor | `moks-executor` | `roboshop-demo-moks-executor` | Executor, its identity and action RBAC, database-backup Roles, Velero write Role, diagnostics namespace and controls |
| terminal | `moks-terminal` | none, the terminal stays off on EKS | Gateway, session identities, terminal namespace, quota and NetworkPolicy |
| routing | `moks-routing` | `roboshop-demo-moks-routing` | mk4: Traefik Ingresses and Middleware. EKS: VirtualService, PeerAuthentication, AuthorizationPolicies |

Three more wait in `deploy/argocd/<environment>/optional/`, because each would be empty today. The root reads `apps/` only.

| Group | Application | Move it to `apps/` when |
|---|---|---|
| migrate | `moks-migrate`, `roboshop-demo-moks-migrate` | `secrets.split` is true |
| opensearch | `moks-opensearch`, `roboshop-demo-moks-opensearch` | `rag.backend` is `opensearch` with no `rag.opensearch.url` |
| reranker | `moks-reranker`, `roboshop-demo-moks-reranker` | `reranker.enabled` is true (docs/RERANKING.md) |
| backup | `moks-backup`, `roboshop-demo-moks-backup` | `backup.enabled` is true |

`scripts/validate-argocd-split.py` fails if a group renders something and has no Application in `apps/`, or the reverse. Velero stays its own Application in `deploy/argocd/velero.yaml`. Pinecone and Amazon OpenSearch Service are external, so neither is an Application. The unrelated RoboShop shopping widget keeps the names `moks` and `roboshop-demo-moks`.

### Why some boundaries are where they are

- **`moks-metrics` and the `moks` NetworkPolicy are in foundation** because both select `app: moks`, which is every MOKS pod, not just the API.
- **The executor's ServiceAccount is in executor, and the gateway's is in terminal.** Each is the identity of that component alone. The shared `moks` account, or `moks-api`, `moks-worker`, `moks-migrate` and `moks-backup` when split, stay in foundation.
- **`allow-nothing` and every ALLOW rule stay together in routing, even the one for PostgreSQL.** Syncing a deny rule without its allows would cut PostgreSQL off. Rolling routing back affects the whole namespace, so diff it with care.
- **`Namespace/velero` and `moks-velero-reader` are in foundation**, because the reader is bound to the shared identities. `moks-executor-velero` is in executor.
- **`moks-config` is hashed by every consumer but rendered only by foundation.** The hash source moved unchanged to `templates/_configmap.yaml`, a partial that is never rendered itself. The checksums therefore match the old ones exactly, and moving ownership restarts nothing.

### Dependencies

These cross Application boundaries, and Argo CD does not enforce them. The root's sync waves would not help: the children have no automated sync, so a root sync starts none of them, and Argo CD does not assess the health of child Applications by default. A Synced, Healthy root says only that the Application objects were written. No parent health customization was added, because nothing here waits on the root. The order is the manual sequence in [section 6](#6-child-adoption-and-sync-order).

| Needs | Before it | Why |
|---|---|---|
| foundation | everything | Namespace labels, `moks-config`, `moks-seed`, ServiceAccounts, `Namespace/velero` |
| routing | database, on a fresh EKS install | So that no pod ever runs before STRICT mTLS and deny-by-default exist. The ALLOW rule for PostgreSQL arrives in the same sync |
| database | api, worker, executor, migrate, terminal, backup | Migrations and `wait-schema` need PostgreSQL. Check readiness, because a child that is merely Synced proves nothing |
| api, or migrate when split | terminal | The gateway always waits in `wait-schema` for the schema |
| migrate, when split | api, worker, executor | Runtime pods wait in `wait-schema`, then crash-loop until the Job finishes |
| opensearch, when bundled | api, worker | Search and reindexing need it |


## 1. Old-to-new ownership

Generated by `scripts/validate-argocd-split.py --ownership-table` across its scenario matrix. "if enabled" means a value switches the resource on in that environment, and `-` means the matrix did not render it there. Group `api` is the Application `moks-api` on mk4 and `roboshop-demo-moks-api` on EKS, and so on.

Namespace `moks` itself is not rendered by the chart: the old Application's `managedNamespaceMetadata` set its labels, and now only foundation does. Secrets stay out of band, and no Application owns one.

| New group | Kind | Namespace | Name | Old owner on mk4 | Old owner on EKS |
|---|---|---|---|---|---|
| foundation | ClusterRole | (cluster) | `moks-reader` | moks-portal | roboshop-demo-moks-portal |
| foundation | ClusterRoleBinding | (cluster) | `moks-reader` | moks-portal | roboshop-demo-moks-portal |
| foundation | ConfigMap | moks | `moks-config` | moks-portal | roboshop-demo-moks-portal |
| foundation | ConfigMap | moks | `moks-seed` | moks-portal | roboshop-demo-moks-portal |
| foundation | ConfigMap | moks | `moks-skills` | if enabled | if enabled |
| foundation | Namespace | (cluster) | `velero` | moks-portal | if enabled |
| foundation | NetworkPolicy | moks | `moks` | moks-portal | roboshop-demo-moks-portal |
| foundation | Role | moks | `alloy-logs` | moks-portal | - |
| foundation | Role | moks | `ecr-refresh` | moks-portal | - |
| foundation | Role | velero | `moks-velero-reader` | moks-portal | if enabled |
| foundation | RoleBinding | moks | `alloy-logs` | moks-portal | - |
| foundation | RoleBinding | moks | `ecr-refresh` | moks-portal | - |
| foundation | RoleBinding | moks | `moks-reader` | if enabled | if enabled |
| foundation | RoleBinding | velero | `moks-velero-reader` | moks-portal | if enabled |
| foundation | Service | moks | `moks-metrics` | moks-portal | roboshop-demo-moks-portal |
| foundation | ServiceAccount | moks | `moks` | moks-portal | roboshop-demo-moks-portal |
| foundation | ServiceAccount | moks | `moks-api` | if enabled | if enabled |
| foundation | ServiceAccount | moks | `moks-backup` | if enabled | if enabled |
| foundation | ServiceAccount | moks | `moks-migrate` | if enabled | if enabled |
| foundation | ServiceAccount | moks | `moks-worker` | if enabled | if enabled |
| database | NetworkPolicy | moks | `moks-postgres` | moks-portal | roboshop-demo-moks-portal |
| database | Service | moks | `moks-postgres` | moks-portal | roboshop-demo-moks-portal |
| database | StatefulSet | moks | `moks-postgres` | moks-portal | roboshop-demo-moks-portal |
| migrate | Job | moks | `moks-migrate-<hash>` | if enabled | if enabled |
| opensearch | NetworkPolicy | moks | `moks-opensearch` | if enabled | if enabled |
| opensearch | Service | moks | `moks-opensearch` | if enabled | if enabled |
| opensearch | StatefulSet | moks | `moks-opensearch` | if enabled | if enabled |
| api | Deployment | moks | `moks-api` | moks-portal | roboshop-demo-moks-portal |
| api | PodDisruptionBudget | moks | `moks-api` | moks-portal | - |
| api | Service | moks | `moks-api` | moks-portal | roboshop-demo-moks-portal |
| worker | Deployment | moks | `moks-worker` | moks-portal | roboshop-demo-moks-portal |
| worker | Deployment | moks | `moks-worker-detection` | if enabled | if enabled |
| worker | Deployment | moks | `moks-worker-knowledge` | if enabled | if enabled |
| executor | ClusterRole | (cluster) | `moks-executor` | moks-portal | roboshop-demo-moks-portal |
| executor | ClusterRole | (cluster) | `moks-executor-cluster-identity` | moks-portal | roboshop-demo-moks-portal |
| executor | ClusterRoleBinding | (cluster) | `moks-executor-cluster-identity` | moks-portal | roboshop-demo-moks-portal |
| executor | Deployment | moks | `moks-executor` | moks-portal | roboshop-demo-moks-portal |
| executor | LimitRange | moks-diagnostics | `diagnostics` | moks-portal | roboshop-demo-moks-portal |
| executor | Namespace | (cluster) | `moks-diagnostics` | moks-portal | roboshop-demo-moks-portal |
| executor | NetworkPolicy | moks-diagnostics | `diagnostics` | moks-portal | roboshop-demo-moks-portal |
| executor | ResourceQuota | moks-diagnostics | `diagnostics` | moks-portal | roboshop-demo-moks-portal |
| executor | Role | jithendar | `moks-database-backup` | moks-portal | - |
| executor | Role | moks | `moks-database-backup` | moks-portal | roboshop-demo-moks-portal |
| executor | Role | moks-diagnostics | `moks-diagnostics` | moks-portal | roboshop-demo-moks-portal |
| executor | Role | roboshop-ai | `moks-database-backup` | moks-portal | roboshop-demo-moks-portal |
| executor | Role | velero | `moks-executor-velero` | moks-portal | if enabled |
| executor | RoleBinding | jithendar | `moks-database-backup` | moks-portal | - |
| executor | RoleBinding | moks | `moks-database-backup` | moks-portal | roboshop-demo-moks-portal |
| executor | RoleBinding | moks-diagnostics | `moks-diagnostics` | moks-portal | roboshop-demo-moks-portal |
| executor | RoleBinding | moks-lab | `moks-executor` | moks-portal | - |
| executor | RoleBinding | roboshop-ai | `moks-database-backup` | moks-portal | roboshop-demo-moks-portal |
| executor | RoleBinding | velero | `moks-executor-velero` | moks-portal | if enabled |
| executor | ServiceAccount | moks | `moks-executor` | moks-portal | roboshop-demo-moks-portal |
| terminal | ClusterRole | (cluster) | `moks-terminal-reader` | moks-portal | if enabled |
| terminal | Deployment | moks | `moks-terminal` | moks-portal | if enabled |
| terminal | LimitRange | moks-terminals | `terminals` | moks-portal | if enabled |
| terminal | Namespace | (cluster) | `moks-terminals` | moks-portal | if enabled |
| terminal | NetworkPolicy | moks-terminals | `terminals` | moks-portal | if enabled |
| terminal | ResourceQuota | moks-terminals | `terminals` | moks-portal | if enabled |
| terminal | Role | moks-terminals | `ecr-refresh` | moks-portal | - |
| terminal | Role | moks-terminals | `moks-terminal-gateway` | moks-portal | if enabled |
| terminal | RoleBinding | moks-lab | `moks-terminal-reader` | moks-portal | if enabled |
| terminal | RoleBinding | moks-terminals | `ecr-refresh` | moks-portal | - |
| terminal | RoleBinding | moks-terminals | `moks-terminal-gateway` | moks-portal | if enabled |
| terminal | Service | moks | `moks-terminal` | moks-portal | if enabled |
| terminal | ServiceAccount | moks | `moks-terminal` | moks-portal | if enabled |
| terminal | ServiceAccount | moks-terminals | `moks-terminal-session` | moks-portal | if enabled |
| routing | AuthorizationPolicy | moks | `allow-nothing` | - | roboshop-demo-moks-portal |
| routing | AuthorizationPolicy | moks | `moks` | - | roboshop-demo-moks-portal |
| routing | AuthorizationPolicy | moks | `moks-postgres` | - | roboshop-demo-moks-portal |
| routing | Ingress | moks | `moks` | moks-portal | - |
| routing | Ingress | moks | `moks-http` | moks-portal | - |
| routing | Middleware | moks | `https-redirect` | moks-portal | - |
| routing | PeerAuthentication | moks | `default` | - | roboshop-demo-moks-portal |
| routing | VirtualService | moks | `moks` | - | roboshop-demo-moks-portal |
| backup | ConfigMap | moks | `moks-backup-scripts` | if enabled | if enabled |
| backup | CronJob | moks | `moks-backup` | if enabled | if enabled |

## 2. Backup and preflight

The steps below use these variables, so set one block. The commands work in bash and zsh. Everything in this section is read-only except the dump.

```bash
# EKS
export ARGO=mk4 CTX=roboshop-demo ENV=eks OLD=roboshop-demo-moks-portal PREFIX=roboshop-demo-moks
ORDER=(foundation routing database api worker executor)

# mk4
export ARGO=mk4 CTX=mk4 ENV=minikube OLD=moks-portal PREFIX=moks
ORDER=(foundation database api worker executor terminal routing)

export BACKUP="$HOME/moks-argocd-split/$ENV-$(date +%Y%m%d-%H%M%S)"; mkdir -p "$BACKUP"; chmod 700 "$BACKUP"
```

**Find out which case you are in, first.** On 2026-11-01 the two clusters were different:

| Cluster | Seen, read-only | Which path |
|---|---|---|
| EKS | `roboshop-demo-moks-portal` Synced and Healthy, no automated sync, no finalizer, 33 tracked resources that all match the render | Full adoption: sections 2 to 7 |
| mk4 | **No `moks-portal` or `velero` Application, and no MOKS workloads:** `moks-portal` was deleted with cascade that morning. Only the Secrets, namespace labels, `data-moks-postgres-0` and `data-moks-opensearch-0` remain. `Namespace/velero` still has a `moks-portal:` tracking ID | No adoption: [section 6, mk4 with no running portal](#mk4-with-no-running-portal) |

```bash
kubectl --context "$ARGO" -n argocd get application "$OLD" --ignore-not-found -o name
kubectl --context "$CTX" -n moks get deploy,statefulset,pvc
```

Back up the Application and the record of what exists, never the Secrets:

```bash
kubectl --context "$ARGO" -n argocd get application "$OLD" -o json > "$BACKUP/old-app.json"
jq .status.resources "$BACKUP/old-app.json" > "$BACKUP/old-resources.json"
kubectl --context "$ARGO" -n argocd get appprojects moks moks-eks -o yaml > "$BACKUP/projects.yaml"
kubectl --context "$CTX" get namespace moks -o yaml > "$BACKUP/namespace.yaml"
kubectl --context "$CTX" -n moks get deploy,statefulset,service,configmap,serviceaccount,role,rolebinding,networkpolicy -o yaml > "$BACKUP/moks-resources.yaml"

identity() {   # generation moves if a spec changes; uid moves if an object is recreated
  kubectl --context "$CTX" -n moks get deploy,statefulset,pvc -o json | jq -S '[.items[] | {kind, name: .metadata.name, uid: .metadata.uid, generation: .metadata.generation, volume: .spec.volumeName}]'
}
identity > "$BACKUP/identity.json"
kubectl --context "$CTX" get pv -o json | jq -S '[.items[] | select(.spec.claimRef.namespace == "moks") | {name: .metadata.name, claim: .spec.claimRef.name, reclaim: .spec.persistentVolumeReclaimPolicy}]' > "$BACKUP/volumes.json"
database() {
  kubectl --context "$CTX" -n moks exec statefulset/moks-postgres -c postgres -- psql -U moks -d moks -Atc \
    "select version_num from alembic_version; select count(*) from users; select pg_database_size('moks');"
}
database > "$BACKUP/database.txt"
scripts/backup.sh "$CTX" "$BACKUP"     # plain SQL with password hashes: keep it private, delete it when done
```

Each of these must pass. Stop if one does not.

```bash
# Annotation tracking. Empty means Argo CD 3's default, which is annotation. The steps below assume it.
kubectl --context "$ARGO" -n argocd get configmap argocd-cm -o jsonpath='{.data.application\.resourceTrackingMethod}'; echo

# Clean and idle: Synced, Healthy, no automated sync, no finalizer, nothing running
jq -e '.status.sync.status == "Synced" and .status.health.status == "Healthy"
  and .spec.syncPolicy.automated == null and ((.metadata.finalizers // []) | length == 0)
  and .operation == null and .status.operationState.phase != "Running"' "$BACKUP/old-app.json"

# Every tracked resource has exactly one new owner. Prints ORPHAN or NEW lines if not.
scripts/validate-argocd-split.py --compare-live "$ENV" "$BACKUP/old-app.json"
scripts/validate-argocd-split.py

# The chart in master renders what is running: push first, then this must print nothing
argocd app diff "$OLD" --hard-refresh

# No selector relies on the tracking label (the validator checks too)
kubectl --context "$CTX" -n moks get deploy,statefulset,service -o json | jq -e '[.items[] | (.spec.selector | tostring) | select(test("instance"))] | length == 0'
```

`mk4` and `roboshop-demo` must both be reachable, and `argocd` logged in to mk4. The old Application's annotation `Prune=false,Delete=false` protects nothing here. That annotation only applies to a resource managed by another Application, and the old Applications have no parent. What makes retirement safe is that they have no finalizer.

The PostgreSQL volume on mk4 has reclaim policy `Delete`, so deleting that PVC destroys the data. No step here deletes one. To be safer on mk4, first run `kubectl --context mk4 patch pv <volume> -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'`.

## 3. Stop the old Application reconciling

The chart change goes first, and it changes nothing: `resourceGroup` defaults to `all`, which renders exactly what ran before, so the old Application stays Synced.

```bash
# Merge the split to master, then check the old Application still has nothing to do
argocd app diff "$OLD" --hard-refresh     # must print nothing
```

The old Applications have no automated sync, so the controller compares but never writes. A person pressing Sync is the only way anything is written. Tell anyone who might, then confirm, and end any operation in flight:

```bash
kubectl --context "$ARGO" -n argocd get application "$OLD" -o json | jq -e '.spec.syncPolicy.automated == null'
kubectl --context "$ARGO" -n argocd get application "$OLD" -o json | jq '.operation, .status.operationState.phase'
argocd app terminate-op "$OLD"            # only if an operation is Running
```

If automated sync ever appears on it, remove that before anything else:

```bash
kubectl --context "$ARGO" -n argocd patch application "$OLD" --type json -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
```

Do not point the old Application at another path or an empty revision to quieten it. Its resources would then show as needing prune, and one careless sync would delete them. Retiring it in [section 5](#5-retire-the-old-application-without-cascading) is what stops it for good.

## 4. Tracking metadata and FailOnSharedResource

Argo CD on mk4 records ownership in the annotation `argocd.argoproj.io/tracking-id`, as in `roboshop-demo-moks-portal:apps/Deployment:moks/moks-api`.

**New names cannot break selectors.** Annotation tracking leaves labels alone: the live chart label `app.kubernetes.io/instance` is still `moks`, because `releaseName` is `moks`. Every selector in the chart uses `app`, `component` or `pool` instead, and the validator fails if one ever names `app.kubernetes.io/instance`. If tracking were ever switched to labels, Argo CD would rewrite that label to the child's name on top-level metadata only, which still reaches no selector. Do not switch tracking during this migration.

**FailOnSharedResource would stop adoption, as it should.** A child comparing a resource whose tracking ID names another Application raises a SharedResourceWarning, even if that Application is gone, and `FailOnSharedResource=true` then fails the sync. Every child keeps that option, and it is never turned off. The old tracking IDs are cleared instead:

- Do not pass `--sync-option FailOnSharedResource=false`. Options on a sync replace the Application's list rather than adding to it, so it would also drop `PruneLast` and, on foundation, `CreateNamespace`.
- Do not edit a child to remove the option. It is the guard against a second owner for the rest of the Application's life.

`scripts/argocd-clear-tracking.sh` removes the annotation only where it names the given Application, and prints the plan unless you add `--apply`. An `kubectl annotate` changes object metadata, not the pod template, so nothing restarts. Run it after retiring the old Application, and before the root exists, so that no child ever sees the old ID. While an object has no tracking ID it is simply unmanaged and keeps running. A child's first sync writes its own ID.

`Namespace/moks` has no tracking ID. Foundation reapplies the same labels through `managedNamespaceMetadata`, so `istio-injection` and the pod security labels never go away.

## 5. Retire the old Application without cascading

A finalizer is what makes deleting an Application delete its resources. With no `resources-finalizer.argocd.argoproj.io` on it, in any variant, deleting the Application removes only that object. Preflight showed none. Check again immediately before, because this step is the one that can do the most harm. Do not use `argocd app delete` without `--cascade=false`, because its default is to cascade.

```bash
# Must print [] or null. If it lists a finalizer, clear it and check again.
kubectl --context "$ARGO" -n argocd get application "$OLD" -o jsonpath='{.metadata.finalizers}'; echo
kubectl --context "$ARGO" -n argocd patch application "$OLD" --type merge -p '{"metadata":{"finalizers":null}}'   # only if it listed one

kubectl --context "$ARGO" -n argocd delete application "$OLD"

# Nothing moved
identity | diff "$BACKUP/identity.json" -

# See what would be cleared, then do it
scripts/argocd-clear-tracking.sh "$CTX" "$OLD" "$BACKUP/old-resources.json"
scripts/argocd-clear-tracking.sh "$CTX" "$OLD" "$BACKUP/old-resources.json" --apply

identity | diff "$BACKUP/identity.json" -
```

The workloads now have no Argo CD owner, and keep running. Stay here as long as you need.

## 6. Child adoption and sync order

Apply the projects and the root by hand. The root's sync writes the child Applications and starts none of them.

```bash
kubectl --context mk4 apply -f deploy/argocd/project-root.yaml -f "deploy/argocd/$ENV/project.yaml" -f "deploy/argocd/$ENV/root.yaml"
argocd app diff "$PREFIX-root"
argocd app sync "$PREFIX-root"
argocd app list -p "$([[ $ENV == eks ]] && echo moks-eks || echo moks)"
```

Adopt the children one at a time, in the order set in `ORDER`. Read each diff. It may show `argocd.argoproj.io/tracking-id` being added and nothing else. **Anything else means live state differs from Git, so stop.** An identical spec means no restart, and that matters here: a pod template change would also pull whatever `latest` is now.

```bash
adopt() {
  local app="$PREFIX-$1"
  argocd app get "$app" --hard-refresh > /dev/null
  argocd app diff "$app"
  printf 'Sync %s? [y/N] ' "$app"; read -r answer; [[ "$answer" == y ]] || return 1
  argocd app sync "$app" --timeout 600         # never add --prune
  argocd app wait "$app" --sync --health --timeout 600
  identity | diff "$BACKUP/identity.json" - && echo "$app: no object recreated, no spec changed"
}

adopt foundation    # then read the namespace labels back; see below
kubectl --context "$CTX" get namespace moks --show-labels
```

Then one at a time, with the readiness check that matters for that child. A Synced child proves nothing about the next one's dependencies.

```bash
# EKS only, at this point: STRICT and deny-by-default before anything else
adopt routing
kubectl --context "$CTX" -n moks get peerauthentication,authorizationpolicy,virtualservice

adopt database
kubectl --context "$CTX" -n moks rollout status statefulset/moks-postgres --timeout=300s
kubectl --context "$CTX" -n moks exec statefulset/moks-postgres -c postgres -- pg_isready -U moks -d moks

# If secrets.split is true: migrate goes here. See "Upgrades" below.

adopt api
kubectl --context "$CTX" -n moks rollout status deploy/moks-api --timeout=300s

adopt worker
kubectl --context "$CTX" -n moks get deploy -l app.kubernetes.io/component=worker
kubectl --context "$CTX" -n moks rollout status deploy/moks-worker --timeout=300s    # each Deployment, with worker.pools

adopt executor
kubectl --context "$CTX" -n moks rollout status deploy/moks-executor --timeout=300s

# mk4 only
adopt terminal
kubectl --context "$CTX" -n moks rollout status deploy/moks-terminal --timeout=300s
adopt routing
```

Finish with the end-to-end checks, and confirm nothing is shared or waiting for prune:

```bash
argocd app list -p "$([[ $ENV == eks ]] && echo moks-eks || echo moks)"       # all Synced and Healthy
for group in "${ORDER[@]}"; do
  kubectl --context "$ARGO" -n argocd get application "$PREFIX-$group" -o json \
    | jq -r '"\(.metadata.name) \([.status.resources[]] | length) resources, conditions: \(.status.conditions // [] | map(.type))"'
done
curl -fsS https://eks.jithendar.com/moks/api/health      # https://jithendar.com/moks/api/health on mk4
```

The resource counts must add up to what the old Application tracked, which is 33 on EKS.

### mk4 with no running portal

If `moks-portal` is gone, as on 2026-11-01, nothing is adopted. This is a fresh install onto kept data, so skip sections 3 and 5. Pods will start, and each `adopt` step will show real changes rather than an empty diff. `scripts/deploy-mk4.sh` runs everything in this section in order, after a read-only `--check`, and retires the `roboshop-demo-moks-*` Applications left by the deleted EKS cluster.

Three things block it:

- **Namespace `moks-lab` is missing**, but `values-minikube.yaml` writes Roles there (`executor.namespaces`, `terminal.kubernetes.namespaces`), so the executor and terminal syncs fail. `lab/00-namespace.yaml` creates it.
- **`ecr-pull` has expired.** ECR tokens last 12 hours, and the refresher, `roboshop-ai/ecr-refresh` from the tools repository, is gone with its namespace. Recreate the namespace and `roboshop-secrets`, which hold only the refresher on mk4, then install the CronJob and run it once, or new pods will not pull.
- **`data-moks-postgres-0` holds the only copy of the mk4 investigations, and its PV has reclaim policy `Delete`.** Deleting the claim or the namespace loses them. It binds to the new StatefulSet by name, on `mk4-m04`. Switch it to `Retain` first, as in section 2.

`data-moks-opensearch-0` is unused while `rag.backend` is `pinecone`. Leave it.

```bash
# Only Namespace/velero still names the old Application
echo '[{"group":"","kind":"Namespace","name":"velero"}]' > "$BACKUP/old-resources.json"
scripts/argocd-clear-tracking.sh mk4 moks-portal "$BACKUP/old-resources.json" --apply
```

Then apply the projects and root as above, and sync foundation and database. **Before the API starts**, take a dump, because the first API pod runs any pending migrations against this old data:

```bash
adopt foundation
adopt database                      # answer y; the diff is the whole StatefulSet
kubectl --context mk4 -n moks rollout status statefulset/moks-postgres --timeout=300s
kubectl --context mk4 -n moks get pvc data-moks-postgres-0 -o jsonpath='{.spec.volumeName}{"\n"}'    # still pvc-091ad61a-...
database; scripts/backup.sh mk4 "$BACKUP"
```

Then continue with `api`, `worker`, `executor`, `terminal`, `routing`. `identity | diff` will differ here, because new objects are being created. Apply `deploy/argocd/velero.yaml` too if you want Velero back.

## 7. Verify PostgreSQL kept its identity and data

After `adopt database`, and again at the end. A pass means the same StatefulSet, the same claim, the same volume and the same rows:

```bash
identity | diff "$BACKUP/identity.json" -       # uid and generation of moks-postgres, uid and volume of data-moks-postgres-0

kubectl --context "$CTX" -n moks get statefulset moks-postgres -o json \
  | jq '{uid: .metadata.uid, serviceName: .spec.serviceName, selector: .spec.selector, claims: [.spec.volumeClaimTemplates[].metadata.name]}'
# serviceName moks-postgres, selector {app: moks-postgres}, claims ["data"]

kubectl --context "$CTX" -n moks get pvc data-moks-postgres-0 -o jsonpath='{.metadata.uid} {.spec.volumeName} {.status.phase}{"\n"}'
kubectl --context "$CTX" -n moks get pod moks-postgres-0 -o jsonpath='{.metadata.uid} {.status.containerStatuses[0].restartCount}{"\n"}'

kubectl --context "$CTX" get pv -o json | jq -S '[.items[] | select(.spec.claimRef.namespace == "moks") | {name: .metadata.name, claim: .spec.claimRef.name, reclaim: .spec.persistentVolumeReclaimPolicy}]' | diff "$BACKUP/volumes.json" -

database | diff "$BACKUP/database.txt" -        # schema version and users match. Database size may grow a little
```

The size may differ, because the application keeps writing. The schema version and the user count should not, unless someone signed up meanwhile. The claim is named `data-moks-postgres-0` and its annotation `Prune=false,Delete=false` is unchanged, because the volume claim template renders byte for byte as before.

## 8. Rollback

Each stage undoes only what that stage did. None needs a pod restart. No stage deletes a workload or a PVC.

**Stage A: merged, old Application still there.** Nothing live changed, and the old Application renders the same either way:

```bash
git revert <merge commit> && git push      # your call: nothing is using the split yet
```

**Stage B: old Application retired, no root yet.** Put it back. It shows OutOfSync until the sync restores the tracking IDs:

```bash
LEGACY=deploy/argocd/legacy/$([[ $ENV == eks ]] && echo application-eks.yaml || echo application.yaml)
kubectl --context mk4 apply -f "$LEGACY"
argocd app diff "$OLD" --hard-refresh
argocd app sync "$OLD"                     # never add --prune
identity | diff "$BACKUP/identity.json" -
```

**Stage C: root or any child exists, however far adoption got.** The same whether one child or all have synced. Order matters: remove the root first, then the children, then the tracking, and only then the old Application, so that nothing has two owners.

```bash
# None of these may list a finalizer. Stop if one does.
kubectl --context mk4 -n argocd get applications -o json | jq -r --arg prefix "$PREFIX-" \
  '.items[] | select(.metadata.name | startswith($prefix)) | "\(.metadata.name) \(.metadata.finalizers // [])"'

kubectl --context mk4 -n argocd delete application "$PREFIX-root"

for group in "${ORDER[@]}" migrate opensearch backup; do
  app="$PREFIX-$group"
  kubectl --context mk4 -n argocd get application "$app" --ignore-not-found -o json > "$BACKUP/$app.json"
  [[ -s "$BACKUP/$app.json" ]] || continue
  jq '.status.resources // []' "$BACKUP/$app.json" > "$BACKUP/$app.resources.json"
  kubectl --context mk4 -n argocd delete application "$app"
  scripts/argocd-clear-tracking.sh "$CTX" "$app" "$BACKUP/$app.resources.json" --apply
done

LEGACY=deploy/argocd/legacy/$([[ $ENV == eks ]] && echo application-eks.yaml || echo application.yaml)
kubectl --context mk4 apply -f "$LEGACY"
argocd app diff "$OLD" --hard-refresh      # tracking IDs only
argocd app sync "$OLD"                     # never add --prune
identity | diff "$BACKUP/identity.json" -
```

The old Application sets no `resourceGroup`, so the default `all` renders everything again, and the chart can stay on master. The tracking script skips a resource a child never wrote its ID on.

**Project `moks-root`** can stay, because it only admits the root. To remove it: `kubectl --context mk4 -n argocd delete appproject moks-root`.

**A child that was already healthy and later went wrong** does not need any of this. See [Rolling back one component](#rolling-back-one-component).

**Data.** No step touches the database. If `database | diff` shows a real loss, stop syncing and restore from the dump in `$BACKUP` (see the [operator guide](OPERATOR_GUIDE.md)). Do not delete the PVC to start again.

## Running the split

### Fresh install

Push first, because everything reads `master`. Create the Secrets as in the [operator guide](OPERATOR_GUIDE.md), then:

```bash
kubectl --context mk4 apply -f deploy/argocd/project-root.yaml -f deploy/argocd/minikube/project.yaml -f deploy/argocd/minikube/root.yaml
argocd app sync moks-root

kubectl --context mk4 apply -f deploy/argocd/project-root.yaml -f deploy/argocd/eks/project.yaml -f deploy/argocd/eks/root.yaml
argocd app sync roboshop-demo-moks-root
```

Then sync the children in the order and with the checks in [section 6](#6-child-adoption-and-sync-order).

### Changing shared configuration

Every consumer hashes the ConfigMaps, so a change shows up as OutOfSync on foundation and on each consumer. Foundation alone only updates the ConfigMap, and pods keep their old environment until their own child syncs. Sync foundation first.

| What changed | Sync, in this order |
|---|---|
| `config.*`, `publicUrl`, `basePath`, `seed`, `customSkills`, `databaseBackups.namespaces`, `diagnostics.namespace` | foundation, then api, worker, executor, terminal. `basePath` also needs routing. `databaseBackups` and `diagnostics` also change executor's own resources |
| `rag.backend`, `rag.opensearch.url`, `rag.pinecone` | foundation (ConfigMap and NetworkPolicy), opensearch if bundled, then api, worker, executor, terminal |
| `image.*`, `imagePullSecrets` | api, worker, executor, terminal, plus migrate and backup when present. Sync migrate first |
| `networkPolicy.*` | foundation, database, executor, terminal, opensearch if bundled |
| `postgres.*` | database. Never change `postgres.storage` or the volume claim template on a live StatefulSet: they are immutable, and the sync fails |
| `worker.*`, `api.*`, `executor.*`, `terminal.*` | That child only |
| `istio.*`, `ingress.*` | routing. `istio.mtls` also changes database |
| `terminal.enabled` | terminal, then routing, which adds the route. Foundation does not change |
| `secrets.split` | See [SECRET_SPLIT.md](SECRET_SPLIT.md) |

Pods read a ConfigMap only at start, so skipping a consumer leaves it on the old configuration, shown as OutOfSync until you sync it. The `moks-backup` CronJob has no checksum and reads `moks-config` at each run. With `secrets.split` true, a `seed` change needs the migration Job to run again, because only the Job seeds.

### Upgrades and migrations

**With `secrets.split` false, as on both clusters today,** each API, worker and executor pod runs `moks migrate && moks seed` in its init container. `moks migrate` holds a PostgreSQL advisory lock, so concurrent pods are safe. No Job exists. The terminal gateway only waits in `wait-schema`. A release is a restart, in this order, so the schema moves before anything depends on it:

```bash
kubectl --context "$CTX" -n moks exec statefulset/moks-postgres -c postgres -- pg_isready -U moks -d moks
kubectl --context "$CTX" -n moks rollout restart deploy/moks-api
kubectl --context "$CTX" -n moks rollout status deploy/moks-api --timeout=300s
kubectl --context "$CTX" -n moks rollout restart deploy/moks-worker deploy/moks-executor
kubectl --context "$CTX" -n moks rollout status deploy/moks-worker --timeout=300s
kubectl --context "$CTX" -n moks rollout status deploy/moks-executor --timeout=300s
kubectl --context "$CTX" -n moks rollout restart deploy/moks-terminal    # mk4
```

**With `secrets.split` true,** only `moks-migrate` holds the schema owner, and runtime pods wait in `wait-schema`. Move its file into `apps/`, and sync it before anything that waits on it.

The Job's name is `moks-migrate-<hash>`, and the hash covers only the image string. That makes two traps:

- **With a mutable tag the name never changes.** After CI moves `latest`, the Job is still the finished one, so its Application stays Synced and no migration runs. New pods then wait in `wait-schema` for 5 minutes, fail, and retry. Both clusters run `latest`. So delete the finished Job, which holds no data, and sync to create it again. `pullPolicy: Always` makes it pull the new image.
- **With a digest, each release leaves the previous Job behind** in the migrate Application, shown as needing prune. Delete that Job by hand. Never prune.

```bash
# mutable tag: every release. digest: change the digest in Git, then delete only the old Job.
kubectl --context "$CTX" -n moks delete job -l app.kubernetes.io/component=migrate
argocd app sync "$PREFIX-migrate"          # never add --prune
kubectl --context "$CTX" -n moks wait --for=condition=complete job -l app.kubernetes.io/component=migrate --timeout=900s

# Then the runtime pods, which now find the schema ready
kubectl --context "$CTX" -n moks rollout restart deploy/moks-api deploy/moks-worker deploy/moks-executor
kubectl --context "$CTX" -n moks rollout status deploy/moks-api --timeout=300s
```

A Job's pod template is immutable, so a chart change to it under an unchanged name also fails to sync until the old Job is deleted. Syncing `moks-api` before the Job finishes does no damage, but its pods crash-loop until the schema arrives.

**Check on EKS before setting `secrets.split` true there.** The namespace injects sidecars, and the Job has no `sidecar.istio.io/inject: "false"`. A sidecar can keep a Job from ever completing, and `automountServiceAccountToken: false` may stop it from starting. This predates the split and was not changed. The same applies to the backup CronJob. Test it on EKS first.

### Rolling back one component

For a chart change. Images are rolled back by moving `latest`, as in [RELEASES.md](RELEASES.md), which no Application sees.

```bash
argocd app history moks-api
argocd app rollback moks-api <history id>       # without --prune
```

A rollback pins that child's render to an older revision until the next sync. If the older render has a different `checksum/config`, the pods restart, and they pull `latest`. Rolling back foundation can leave pods hashed against a newer ConfigMap than the live one, so check its consumers too. Roll back routing with care: see [Why some boundaries are where they are](#why-some-boundaries-are-where-they-are).

### Adding or removing a child

Enable the feature in the values, move the file between `optional/` and `apps/`, run the validator, push, and sync the root. To remove a child, move the file back and **delete the Application by hand**. The root's `Prune=false,Delete=false` annotation means it will not, and the root will show it as extraneous until then. With no finalizer, its resources stay until you remove them. Never prune the root to do this.

Add a new namespace in `executor.namespaces`, `databaseBackups.namespaces` or similar to the environment's `project.yaml` too, and apply that by hand. Projects are not managed by the root, so a root cannot widen its own permissions.

## Security

- **Project `moks-root` is the only change in what Argo CD may write.** It admits only `argoproj.io/Application`, only in `argocd` on mk4, and only from this repository. Giving the roots their own project keeps that out of `moks` and `moks-eks`. An Application can name any project, so anyone who can write to `deploy/argocd/*/apps` in `master` can define one: review those paths like code.
- **`moks` and `moks-eks` are unchanged** apart from comments and their file location: same repository, destinations and kinds, and still no Secrets.
- **Rendered resources are unchanged**, so RBAC, NetworkPolicies, STRICT mTLS and AuthorizationPolicies are as they were. The validator compares each with the pre-split chart.

## Validation

Run `scripts/validate-argocd-split.py` before pushing a change to the chart or to `deploy/argocd`. CI runs it on every pull request. It needs `helm` and PyYAML.

For each of mk4 and EKS, across terminal on and off, bundled and external PostgreSQL, every RAG backend, worker pools, backups, both secret modes and more, it renders every group and fails on a resource with two owners or none, or an unexpected addition. It requires each resource to deep-equal the whole-chart render, and with `--baseline <chart copy>` the pre-split chart as well. It also checks each Application's path, value file, project and destination, whether the project admits every kind and namespace it renders, and the safety settings: no automated sync, no finalizer, `FailOnSharedResource=true`, one namespace owner.

| Proved by rendering and static checks | Only a live run can prove |
|---|---|
| Same resources, same content, one owner each, in 22 scenarios on 2 environments, rendered with Helm 4.3 | Argo CD renders the same result with the Helm it bundles, and the projects admit each sync |
| Every Application and project passed `kubectl apply --dry-run=server` on mk4 (nothing created) | The controller acts on them as described here |
| Same selectors, RBAC, references, network policies, namespace labels, storage identities, checksums | A child's first sync shows no diff beyond tracking, and restarts nothing |
| Application paths, value files, projects, destinations | FailOnSharedResource stays quiet after tracking is cleared |
| Live EKS resources tracked on 2026-11-01 equal the render, 33 of 33 (read-only) | PostgreSQL and its data after adoption, sections 6 and 7 |
