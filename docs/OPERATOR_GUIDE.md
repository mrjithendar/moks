# Operator guide

Commands are listed in the order to run them. The cluster context is `mk4`, the AWS account is
`881026659929` and the region is `us-east-1`, matching the jithendar.com setup. Nothing here has
been run against the cluster yet; each step says what it changes.

## 0. Prerequisites

- `kubectl`, `helm` ≥ 3.14, `aws` CLI, `argocd` CLI (optional), and access to `mk4`.
- An `ecr-refresh` CronJob in `roboshop-ai` (tools repository) already maintains `ecr-pull`
  Secrets. MOKS reuses it.
- Capacity: MOKS needs about 0.5 CPU and 1 GiB in steady state (2 API pods, 1 worker,
  PostgreSQL). Each mk4 node has ~32 GiB, so OpenSearch (~2.5 GiB, Phase 2) also fits.

## 1. AWS resources and CI (one time, Terraform)

```sh
cd deploy/terraform
terraform init
terraform plan -var-file=tfvars/mk4.tfvars -out=moks.tfplan     # ECR repo, GitHub OIDC + CI role, runtime IAM user
terraform apply moks.tfplan
gh variable set AWS_ROLE_ARN --repo mrjithendar/moks --body "$(terraform output -raw ci_role_arn)"
```

This creates the immutable `moks` ECR repository and a CI role that GitHub Actions assumes over
OIDC, so there are no stored access keys and only `master` can push. It also creates an IAM user
for MOKS's Bedrock calls; that user has no key yet (step 8). Details are in
[../deploy/terraform/README.md](../deploy/terraform/README.md), including how to check that
mk4's `ecr-refresh` identity may pull the new repository.

CI (`.github/workflows/ci.yml`) tests every push, then on `master` builds the arm64 image and
pushes an image tagged with its commit, and tags in ECR are immutable. It
never touches the cluster. Commit and push this repository (`github.com/mrjithendar/moks`) and
wait for the `release` job, whose summary prints the commit and image digest.

## 2. Namespace, pull secret and refresher

```sh
kubectl --context mk4 create namespace moks
# First ecr-pull Secret (refresh.py only patches existing Secrets):
kubectl --context mk4 -n moks create secret docker-registry ecr-pull \
  --docker-server=881026659929.dkr.ecr.us-east-1.amazonaws.com --docker-username=AWS \
  --docker-password="$(aws ecr get-login-password --region us-east-1)"
```

In the **tools** repository, add `moks` to the `TARGET_NAMESPACES` env of
`minikube/ecr-refresh/roboshop-ai-ecr-refresh-cronjob.yaml`. The chart already creates the
`ecr-refresh` Role and RoleBinding in `moks` (`ecrRefresh.enabled`). Commit and apply that change
the way you apply the tools repository.

## 3. Secrets (never committed)

```sh
scripts/create-secrets.sh mk4      # moks-postgres + moks-secrets (database URL, session secret)
```

It refuses to overwrite existing Secrets. Add provider credentials later as `MOKS_CRED_*` keys
(see step 8).

## 4. Roll out a new image

CI tags each image with its commit and opens a pull request that pins its digest. Merging that pull request and syncing Argo CD by hand is the rollout, and reverting it is the rollback. See [RELEASES.md](RELEASES.md).

## 5. Argo CD project and application

```sh
kubectl --context mk4 apply -f deploy/argocd/project.yaml -f deploy/argocd/application.yaml
argocd app diff moks-portal          # or the Argo CD UI at https://jithendar.com/argocd
argocd app sync moks-portal          # never tick "Prune" for this app
kubectl --context mk4 -n moks rollout status deploy/moks-api deploy/moks-worker
```

The `moks` AppProject only admits this repository, namespace `moks`, the listed kinds and the
chart's ClusterRole/ClusterRoleBinding, and it cannot touch Secrets. The PostgreSQL PVC carries
`Prune=false,Delete=false`. Like the other Applications on mk4, `moks-portal` has no automated
sync. As with roboshop-ai, `kubectl apply` of a changed Application file is what updates its live
spec.

## 6. First administrator

The site admits nobody until a user exists (local mode has no setup page):

```sh
kubectl --context mk4 -n moks exec -it deploy/moks-api -c api -- \
  moks create-user you@example.com --password-prompt --grant admin
```

Grant others with `moks grant <email> <role>[:<env>[:<ns>]]` or in **Users & roles**.

To switch to OIDC (for example Google), create a web OAuth client with redirect URI
`https://jithendar.com/moks/api/auth/oidc/callback`. Set `config.authMode: oidc`,
`config.oidcIssuer: https://accounts.google.com`, `config.oidcClientId` and
`config.bootstrapAdmins` in `values-minikube.yaml`, and add the client secret:

```sh
kubectl --context mk4 -n moks patch secret moks-secrets --type merge \
  -p '{"stringData":{"MOKS_OIDC_CLIENT_SECRET":"<secret>"}}'
```

## 7. Verify `/moks`

```sh
curl -fsS https://jithendar.com/moks/api/health                     # {"status":"ok",...}
curl -fsSI https://jithendar.com/moks | grep -i location            # 308 -> /moks/
curl -fsS https://jithendar.com/moks/investigations/x | grep -q 'id="root"' && echo deep-link-ok
curl -fsS https://jithendar.com/ | head -c 100                      # site unaffected
```

In the browser: sign in, open **Overview** (health loads by itself and refreshes every 5 seconds), start an investigation and watch
it stream. Refresh the investigation page, which is a deep link. In **Connectors**, press **Test**
on each connector.

## 8. LLM provider (optional; MOKS works rule-based without one)

**Administration → LLM providers → Add** sets the provider, model, budget and prices, then
**Test** and **Activate** it. Credentials:

- OpenAI or Gemini: `kubectl -n moks patch secret moks-secrets --type merge -p '{"stringData":{"MOKS_CRED_OPENAI_API_KEY":"..."}}'`,
  then `kubectl -n moks rollout restart deploy/moks-api deploy/moks-worker`. Reference it as
  *environment variable* `MOKS_CRED_OPENAI_API_KEY`.
- Bedrock on mk4: `scripts/create-runtime-key.sh mk4` creates the access key of the Terraform
  user `moks-mk4-runtime` (allowed to invoke one inference profile only) and writes it into
  `moks-secrets`. Then add provider `bedrock`, region `us-east-1` and model
  `us.amazon.nova-2-lite-v1:0`. On EKS use IRSA or Pod Identity instead.
- Ollama: set the endpoint, for example `http://ollama.ollama:11434`, and allow it in
  `networkPolicy.inCluster`.

## 9. Observability of MOKS

In the **tools** repository:

1. Prometheus: add the job from [../deploy/grafana/prometheus-scrape.yaml](../deploy/grafana/prometheus-scrape.yaml).
2. Grafana: add [../deploy/grafana/moks-dashboard.json](../deploy/grafana/moks-dashboard.json)
   as a `grafana-dashboards-moks` ConfigMap, mounted the same way as `grafana-dashboards-jithendar`.
3. Alloy: add `moks` to `discovery.kubernetes.pods.namespaces`. The chart already grants Alloy
   read access in `moks`.

MOKS sends traces to `otel-collector.observability:4318` (Jaeger). Logs are JSON with
`level`/`event`/`route`/`status`/`durationMs`, which Alloy keeps.

Sync the grafana, prometheus and loki Argo apps after committing; they have no auto-sync.

## 10. Demo lab

See [../lab/README.md](../lab/README.md). It applies to namespace `moks-lab`, and
`kubectl delete namespace moks-lab` removes it.

## 11. Phases 2–4: what changes on mk4 and in what order

The chart changes in this release add pods and RBAC. Review `argocd app diff moks-portal`, then sync
**without prune**. Apply the AppProject first (it now allows `moks-diagnostics`, `moks-lab`,
`ResourceQuota`, `LimitRange` and `Role`):

```sh
kubectl --context mk4 apply -f deploy/argocd/project.yaml
argocd app diff moks-portal && argocd app sync moks-portal
kubectl --context mk4 -n moks rollout status deploy/moks-api deploy/moks-worker deploy/moks-executor
kubectl --context mk4 -n moks rollout status statefulset/moks-opensearch     # rag.backend: opensearch
```

The API's init container runs migration `0002` automatically. Take a backup first
(`scripts/backup.sh mk4`).

1. **Knowledge:** the seed creates the `moks-docs` GitHub source, and the worker syncs it within a
   minute. Add sources under **Runbooks & knowledge** (GitHub repo or uploads). Add a token (needed for
   private repos, and to avoid [rate limits](#github-rate-limits) on public ones): `kubectl -n moks patch secret moks-secrets --type merge -p '{"stringData":{"MOKS_CRED_GITHUB_TOKEN":"…"}}'`.
2. **Grafana:** create a service account with the Viewer role in Grafana (Administration →
   Service accounts), add a token, then:
   `kubectl -n moks patch secret moks-secrets --type merge -p '{"stringData":{"MOKS_CRED_GRAFANA_TOKEN":"glsa_…"}}'`
   and restart the API and worker. **Connectors → Test** should then pass.
3. **FinOps:** set the real monthly server price in `values-minikube.yaml` (`cost_model.monthly_cost_usd`).
   For AWS spend: `terraform apply` (with `finops_enabled = true`), run `scripts/create-runtime-key.sh mk4`,
   then uncomment the `aws_cost` connector. See [FINOPS.md](FINOPS.md).
4. **Remediation:** the executor acts only in `executor.namespaces` (default `[moks-lab]`). To let
   MOKS act elsewhere, add the namespace there **and** to the AppProject destinations. Set the
   environment's policy under **Environments** (`max_replicas`, scale-to-zero, plan TTL).
   `mk4` is marked production, so every action needs a second person with the approver role:
   `moks grant <approver-email> approver:mk4:*`.
5. **Alerts:** to investigate firing Prometheus alerts automatically, tick
   *investigate firing alerts automatically* on the environment; the worker polls every 60 s. For
   Alertmanager or Grafana contact points, set a token:
   `kubectl -n moks patch secret moks-secrets --type merge -p '{"stringData":{"MOKS_ALERT_WEBHOOK_TOKEN":"'$(openssl rand -hex 24)'"}}'`
   and point the webhook at `https://jithendar.com/moks/api/alerts/webhook/mk4` with header
   `Authorization: Bearer <token>`.
6. **Diagnostic sessions:** the chart creates `moks-diagnostics` (quota, limits, egress policy).
   Request sessions under **Diagnostics**; on mk4 (production) each needs an approval.
7. **SRE skills:** 14 built-in skills are active. Add your own through `customSkills` in the values
   ([SKILLS.md](SKILLS.md)).
8. **Evaluation:** **Evaluation → Run lab** (rules mode) should score 1.0. Run it again in LLM mode
   after configuring a provider, to compare.
9. **LLM:** configure providers under **LLM providers**. The mk4 seed adds none. Without an active chat
   model, investigations and **Analyze with AI** use rule-based reports. Set input and output prices there
   so FinOps can show cost as well as tokens. An inactive provider can be deleted; its usage history stays.

## Using Pinecone for vectors

Pinecone can hold the vectors instead of OpenSearch, with keyword search staying in PostgreSQL. It needs no
pods, so `rag.backend: pinecone` frees about 2.5 GiB on mk4.

1. Add `MOKS_CRED_PINECONE_API_KEY` to `moks-secrets`.
2. Set `rag.backend: pinecone`, and `rag.pinecone.cloud` and `region` if not `aws` and `us-east-1`.
3. Sync, then check an embedding model is active and passes **Test** under **LLM providers**.
4. Run a reindex under **Runbooks & knowledge**. MOKS creates an index named `moks-chunks-<id>`, so the key
   needs permission to create indexes. Until the reindex finishes, search is keyword-only.

Pinecone receives vectors, document ids, and the environment and namespace each document is scoped to. It
never receives chunk text. Access is still checked again in PostgreSQL before results are returned. To go
back, set `rag.backend` to its old value; PostgreSQL still holds everything needed to rebuild.

## Investigation conversations

Each investigation is a conversation. Every question starts a new turn with its own evidence, and MOKS sees
the earlier messages as context. Anyone with access to the scope can open one. Replying with **Ask MOKS**
needs `investigation.run` and runs with your own permissions. A person with read access can post notes once
someone in the conversation adds them.

Authors can delete their messages. The person who started a conversation, or an administrator, can delete
any message or the whole conversation. Deleted items can be restored from **Show deleted** under Investigations.

## Editing knowledge sources

Use **Edit** on a source under **Runbooks & knowledge**, or `PUT /api/knowledge/sources/{id}`. Indexed
documents are kept. Name, type and repository cannot change: create a new source for a different
repository. A new scope applies at once, to the database and the search index. New include or exclude
rules apply at the next sync, which removes files that no longer qualify. A token is named by its
variable, and its value stays in `moks-secrets`.

`partial` on a source means the sync finished but some files could not be read; the portal shows how
many. They are retried at the next sync.

**LLM providers → Test** makes a real call. For an embedding model it reports the provider, model and
dimensions. After activating an embedding model, run a reindex to build the vector index: Gemini rate
limits are waited out, and a failed build leaves the current index serving.

## Upgrading to safe execution

Migrations `0004` to `0008` run automatically in the init containers. Take a backup first. They:

- expire plans still pending, approved or queued (their approvals did not cover the target identity);
  create those again
- send any plan caught mid-execution to reconciliation
- give in-cluster connectors an explicit `config.executor`. Others stay read-only until you add one:

```json
{"mode": "eks", "cluster_name": "prod", "region": "us-east-1",
 "executor": {"mode": "eks", "cluster_name": "prod", "region": "us-east-1"}}
```

The chart adds a small ClusterRole so the executor can read the `kube-system` UID. If the Argo project
restricts cluster resources, it already allows ClusterRole and ClusterRoleBinding.

A plan in **reconciliation_required** will not retry. Check the object, then record what happened on
the plan's page (needs the Approver role).

## Kubernetes explorer management

**Workloads → Manage…** offers rollout restart, scale, roll back, change resources and rollout settings
for Deployments. **Services → Manage…** edits the selector or session affinity. Opening a pod offers
**Delete pod**, for pods a controller would replace, and a rollout restart of its Deployment.

Each option creates an action plan, so the usual review applies: production plans need a second person,
and nothing changes until it is executed. Execution only works in the namespaces listed in
`executor.namespaces`, which on mk4 is `moks-lab`. Add a namespace there to manage it. StatefulSets and
DaemonSets can be viewed and investigated but have no actions yet.

## Detection

Detection is off by default. To turn it on:

1. Open **Detection rules** and use **Dry run** on a rule. It runs with your access and writes nothing.
2. Tick **run detection rules** on the environment.
3. Add **maintenance windows** before planned work.

Prometheus rules that need kube-state-metrics report `unknown` until it is installed. mk4 does not have it.

Knowledge sources sync daily by default. Set `MOKS_SYNC_TIMEZONE` to label the schedule; the browser shows
the next run in local time.

## GitHub rate limits

Without a token GitHub allows 60 requests an hour per IP, which a few repositories use up quickly.
Set `MOKS_CRED_GITHUB_TOKEN` in `moks-secrets` and every GitHub source without its own credential
uses it (5,000 requests an hour). A fine-grained token with read-only **Contents** access is enough.

When GitHub rate limits a sync, by 429 or by 403, MOKS stops at once, shows the source as
`rate_limited` and retries when GitHub says it may. Finished files are kept, so the retry resumes
where it stopped. `partial` means the sync finished but some files could not be read; those are
retried on the next sync. `git rejected the credentials (HTTP 403)` now means a real permission
problem with the token.

## Using the portal

- **Overview** shows live health for every environment and refreshes every 5 seconds. Polling
  pauses while the browser tab is hidden. Routine refreshes are not written to the audit log;
  denials and errors are.
- **Metrics, Logs and Kubernetes explorer** start from all environments and all namespaces you can
  read, and each has its own filters. A source that fails shows up as "partial data" and the rest
  of the view still loads.
- **Audit log** filters by action, actor, decision, outcome, environment, namespace, time range,
  resource and details text.
- **Investigate** needs only a question and, optionally, a time window. MOKS uses an environment
  named in the question, otherwise the default one (**default for investigations**), otherwise a
  production one. It then narrows to the namespace from the namespace or workload names in the
  question. Name the environment or namespace in the question when several could match.
- **Adding an environment:** choose the cluster type and MOKS proposes its data sources (EKS:
  EKS, CloudWatch Logs, AWS Cost Explorer; Minikube: this cluster, Prometheus, Loki, cost model).
  Remove any, or add others from **Add data source**. Data sources are created after the environment; if
  one fails, the message says which, and you add it under **Connectors**.

## Local development

```sh
# PostgreSQL (any), Python 3.11+, Node 22+
cd backend && python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest pytest-asyncio && pip install --no-deps -e .
cd ../frontend && npm ci && npm run build
mkdir -p ~/.moks-dev && kubectl config view --minify --flatten --context mk4 > ~/.moks-dev/kubeconfig && chmod 600 ~/.moks-dev/kubeconfig
kubectl --context mk4 -n monitoring port-forward svc/prometheus 19090:9090 &
kubectl --context mk4 -n monitoring port-forward svc/loki 13100:3100 &
MOKS_DATABASE_URL=postgresql+asyncpg://moks@localhost:5432/moks ../dev/run-local.sh
# http://localhost:8080/moks/  (dev sign-in: sre@moks.local is the bootstrap admin)
```

`npm run dev` (port 5173) proxies `/moks/api` to `:8080` for hot reload. Dev mode (`MOKS_AUTH_MODE=dev`)
is password-less and **refuses to start** unless `MOKS_ENV=development`.

## Backup and restore

PostgreSQL is the system of record: users, roles, configuration, investigations, evidence, FinOps
history and the audit log. Credentials live in Kubernetes Secrets, so keep those separately too.

**What protects the data on mk4 depends on the setup below being finished.** All four mk4 nodes are
containers on one disk, so anything stored only in the cluster is lost with that disk.

### Scheduled backups

`backup.enabled` adds an hourly CronJob that dumps the database, encrypts it, uploads it, reads the stored
copy back to prove it matches, and only then applies retention (48 hourly, 30 daily, 12 monthly). The
bucket only ever holds ciphertext. Data written since the last run is not in any backup, so the most you
can lose is about an hour.

1. Create the bucket. For S3, set `backup_bucket_name` in Terraform (with `backup_object_lock_days` if you
   want Object Lock, which can only be chosen at creation). For R2, create it and turn on versioning or
   bucket locks there.
2. Generate the encryption key and store it **outside the cluster** first. Without it no backup can be read:
   `openssl rand -base64 32`
3. Create the Secret, using `--from-env-file` or similar so values stay out of your shell history. Its keys
   are `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `MOKS_BACKUP_ENCRYPTION_KEY`. Name it `moks-backup`.
4. Set `backup.enabled`, `backup.bucket`, and for R2 `backup.endpoint` and `backup.region: auto`.
5. Apply `deploy/argocd/project.yaml`, which now allows CronJobs, then sync.
6. Run one by hand and check the stored copy:
   `kubectl --context mk4 -n moks create job --from=cronjob/moks-backup first-backup`

A warning appears on **Overview** when no backup has succeeded for three hours. For Prometheus alerts, add
`deploy/grafana/prometheus-scrape.yaml` and `deploy/grafana/moks-alerts.yaml` to the tools repository, and
route them to a person. mk4 has no Alertmanager today.

### Restore drill

A backup is not proven until a restore passes. `moks backup drill` proves the newest backup, or one named with `--key`:

```sh
MOKS_DRILL_ADMIN_URL=postgresql://<admin>@<host>/postgres MOKS_BACKUP_BUCKET=... MOKS_BACKUP_ENCRYPTION_KEY=... moks backup drill
```

It needs `pg_restore`, so run it where PostgreSQL client tools are installed. It verifies the checksum, decrypts (which authenticates every chunk), restores into a new `moks_restore_drill_*` database, compares row counts exactly with the manifest taken in the same snapshot as the dump, checks the schema version and application invariants, runs smoke checks including the audit chain, reports recovery time and recovery point, then drops that database. It refuses any other target, so it cannot touch the live database. Exit status is non-zero on any failure.

Differences against a live database are expected and are not a verdict. `moks backup compare` reports them as drift, for information.

**After a real restore,** external search indexes hold nothing that PostgreSQL does not. PostgreSQL stays the authority, so rebuild them with `moks reindex`, or **Rebuild index** under Knowledge. Until then, results are limited to what the index still has, and are still checked against the restored database.

**Keys.** `MOKS_BACKUP_ENCRYPTION_KEY` is the only way to read a backup. Keep it outside the cluster, in a password manager with a second person or a sealed copy. `scripts/decrypt-backup.py` needs nothing else, so a backup is readable when MOKS itself is gone. Losing the key loses every backup, and nothing can recover it.

### Testing a restore by hand

Use a scratch database, never `moks`:

```sh
kubectl --context mk4 -n moks exec statefulset/moks-postgres -- psql -U moks -d postgres -c 'CREATE DATABASE moks_restore_test'
# fetch and decrypt the newest backup, from a pod or machine holding the moks-backup values
moks backup fetch --out /work/moks.dump
pg_restore --no-owner -h moks-postgres -U moks -d moks_restore_test --exit-on-error /work/moks.dump
MOKS_COMPARE_SOURCE_URL=... MOKS_COMPARE_TARGET_URL=... moks backup compare
MOKS_DATABASE_URL=<scratch url> moks verify-audit
kubectl --context mk4 -n moks exec statefulset/moks-postgres -- psql -U moks -d postgres -c 'DROP DATABASE moks_restore_test'
```

`compare` fails if a table is missing or holds more rows than its source. A live source moves on, so a
restore having fewer rows in busy tables is normal and listed under `differ`. Repeat this after any
schema change and at least quarterly.

### Recovery runbook

Use this when the database is lost. Stop writers first, so nothing lands in a half-restored database.

```sh
kubectl --context mk4 -n moks scale deploy/moks-api deploy/moks-worker deploy/moks-executor --replicas=0
kubectl --context mk4 -n moks patch cronjob moks-backup -p '{"spec":{"suspend":true}}'
# recreate the database, keeping any surviving data aside rather than dropping it
kubectl --context mk4 -n moks exec statefulset/moks-postgres -- psql -U moks -d postgres \
  -c 'ALTER DATABASE moks RENAME TO moks_damaged' -c 'CREATE DATABASE moks OWNER moks'
moks backup fetch --out moks.dump        # or scripts/decrypt-backup.py if MOKS is unavailable
pg_restore --no-owner -h <postgres> -U moks -d moks --exit-on-error moks.dump
MOKS_DATABASE_URL=<url> moks verify-audit
kubectl --context mk4 -n moks scale deploy/moks-api --replicas=2
kubectl --context mk4 -n moks scale deploy/moks-worker deploy/moks-executor --replicas=1
kubectl --context mk4 -n moks patch cronjob moks-backup -p '{"spec":{"suspend":false}}'
```

If the whole host is gone, rebuild the cluster and recreate the Secrets first, including the encryption key
from its off-cluster copy. Then run the steps above. Any work since the backup is gone: check the audit log
for the last recorded time. On EKS, use RDS automated snapshots and point-in-time recovery instead.

`scripts/backup.sh` still takes a one-off plain dump to your machine, which is useful before a migration.
It is not encrypted, so delete it when you are done.

## Upgrades and rollback

Migrations run in the API's init container under an advisory lock and are forward-only. Take a
backup before upgrading across a migration. Roll back the application by pinning the previous
image digest (step 4). Roll back data by restoring the backup.

## Troubleshooting

| Symptom | Check |
|---|---|
| `/moks` serves the jithendar.com 404 page | Ingress `moks` exists and Traefik picked it up: `kubectl -n moks get ingress`; check the Traefik logs |
| `/moks/assets/*.js` returns 404 | The image was built with a different `MOKS_BASE_PATH` than `basePath` in values |
| Login loops back to login | `MOKS_PUBLIC_URL` scheme/host must match the browser; cookies are `Secure`, so HTTPS is required |
| "CSRF check failed" | Origin differs from `MOKS_PUBLIC_URL` (for example www vs apex); www redirects to apex on mk4 |
| Investigation stays `queued` | `kubectl -n moks logs deploy/moks-worker`; worker replicas > 0; database reachable |
| Connector test `forbidden` | `moks-reader` ClusterRoleBinding exists; with `rbac.clusterWide=false`, the namespace is listed |
| Connector test `unavailable` | NetworkPolicy egress (`networkPolicy.inCluster`, `apiServer.cidrs`), service DNS name, path prefix (`/prometheus`) |
| Plan execution fails with "changed after review" | The Deployment changed (another rollout, HPA) after the plan was made; create a new plan |
| Plan stays `queued` | `kubectl -n moks logs deploy/moks-executor`; the executor needs a RoleBinding in that namespace (`executor.namespaces`) |
| Knowledge search empty | **Runbooks & knowledge → Sources**: last sync status/error; `kubectl -n moks logs deploy/moks-worker` |
| OpenSearch pod restarting | Needs ~2 GiB; check node memory and `rag.opensearch.nodeName` pinning. `AccessDeniedException` on `data/nodes`: set `rag.opensearch.fixVolumePermissions: true` |
| SSE updates only arrive at the end | Something buffers the response: do not attach compress or buffering middleware to the MOKS router |
