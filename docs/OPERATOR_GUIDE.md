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
pushes `moks:latest` (the only tag; the previous image stays in ECR untagged, for rollback). It
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

mk4 runs `moks:latest` with `imagePullPolicy: Always`. A new push does not change any manifest, so
Argo CD does not redeploy; after each `release` job, restart the pods to pull it:

```sh
kubectl --context mk4 -n moks rollout restart deploy
kubectl --context mk4 -n moks rollout status deploy --timeout=300s
```

To roll back, pin an older image: `scripts/set-image.sh sha256:<digest>` (list them with
`aws ecr describe-images --repository-name moks`), commit and push. Set `image.digest` back to
`""` to follow `:latest` again.

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

In the browser: sign in, open **Overview → Check live health**, start an investigation and watch
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
   minute. Add sources under **Runbooks & knowledge** (GitHub repo or uploads). For private repos,
   add a token: `kubectl -n moks patch secret moks-secrets --type merge -p '{"stringData":{"MOKS_CRED_GITHUB_TOKEN":"…"}}'`.
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
7. **SRE skills:** 13 built-in skills are active. Add your own through `customSkills` in the values
   ([SKILLS.md](SKILLS.md)).
8. **Evaluation:** **Evaluation → Run lab** (rules mode) should score 1.0. Run it again in LLM mode
   after configuring a provider, to compare.

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

What matters is PostgreSQL: users, roles, configuration, investigations, evidence and the audit
log. Credentials live in Kubernetes Secrets, so back those up separately (or recreate them).

```sh
scripts/backup.sh mk4 ./backups            # pg_dump | gzip -> moks-<UTC>.sql.gz
```

Restore into an empty database, for example after recreating the PVC:

```sh
kubectl --context mk4 -n moks scale deploy/moks-api deploy/moks-worker --replicas=0
kubectl --context mk4 -n moks exec -i statefulset/moks-postgres -- psql -U moks -d postgres \
  -c 'DROP DATABASE IF EXISTS moks WITH (FORCE)' -c 'CREATE DATABASE moks OWNER moks'
gunzip -c backups/moks-<UTC>.sql.gz | kubectl --context mk4 -n moks exec -i statefulset/moks-postgres -- psql -U moks -d moks -v ON_ERROR_STOP=1
kubectl --context mk4 -n moks scale deploy/moks-api --replicas=2 && kubectl --context mk4 -n moks scale deploy/moks-worker --replicas=1
kubectl --context mk4 -n moks exec deploy/moks-api -c api -- moks verify-audit
```

The dump includes the audit triggers. Rows are restored with their original hashes, so
`verify-audit` should report `ok: true`. Schedule backups with a CronJob, or copy dumps to R2/S3
as jithendar.com does. On EKS, use RDS automated snapshots and point-in-time recovery.

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
| OpenSearch pod restarting | Needs ~2 GiB; check node memory and `rag.opensearch.nodeName` pinning |
| SSE updates only arrive at the end | Something buffers the response: do not attach compress or buffering middleware to the MOKS router |
