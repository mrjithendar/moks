# Separating credentials and database roles

Until `secrets.split` is true, every MOKS component shares `moks-secrets` and connects as the schema owner, exactly as it always has. Upgrading the chart changes nothing. This guide moves an install to one Secret, ServiceAccount and database role per component.

## What each component holds afterwards

| Component | Secret | ServiceAccount | Database role | Cluster access |
|---|---|---|---|---|
| API | `moks-api-secrets` | `moks-api` | `moks_api` | read-only, no Secrets |
| Worker | `moks-worker-secrets` | `moks-worker` | `moks_worker` | read-only, no Secrets |
| Executor | `moks-executor-secrets` | `moks-executor` | `moks_executor` | writes, per namespace |
| Terminal gateway | `moks-terminal-secrets` (always its own, split or not) | `moks-terminal` | `moks_terminal` | pods and exec in the terminal namespace only |
| Migration job | `moks-migrate-secrets` | `moks-migrate` (no token) | schema owner | none |
| Backup | `moks-backup` | `moks-backup` (no token) | `moks_backup` | none |

Grants live in [`backend/moks/db_roles.py`](../backend/moks/db_roles.py). The API can only insert approvals, cannot write the lease, token or checkpoint of a plan, and a trigger limits which plan status changes it may make.

## Stage 1: add the new Secrets, change nothing yet

Generate four database passwords, then create the Secrets from your password manager. Only the variable names below are fixed. Do not put real values in Git.

```sh
kubectl -n moks create secret generic moks-migrate-secrets \
  --from-literal=MOKS_DATABASE_URL='postgresql+asyncpg://moks:<owner password>@moks-postgres:5432/moks' \
  --from-literal=MOKS_SESSION_SECRET='<same value as today>' \
  --from-literal=MOKS_DB_PASSWORD_API='<new>' --from-literal=MOKS_DB_PASSWORD_WORKER='<new>' \
  --from-literal=MOKS_DB_PASSWORD_EXECUTOR='<new>' --from-literal=MOKS_DB_PASSWORD_BACKUP='<new>'

kubectl -n moks create secret generic moks-api-secrets \
  --from-literal=MOKS_DATABASE_URL='postgresql+asyncpg://moks_api:<new>@moks-postgres:5432/moks' \
  --from-literal=MOKS_SESSION_SECRET='<same value as today>' \
  --from-literal=MOKS_OIDC_CLIENT_SECRET='<if used>' --from-literal=MOKS_ALERT_WEBHOOK_TOKEN='<if used>' \
  --from-literal=MOKS_CRED_R2_ACCESS_KEY_ID='<for the storage test>' --from-literal=MOKS_CRED_R2_SECRET_ACCESS_KEY='<for the storage test>' \
  --from-literal=MOKS_CRED_R2_SSE_KEY='<for the storage test>'

kubectl -n moks create secret generic moks-worker-secrets \
  --from-literal=MOKS_DATABASE_URL='postgresql+asyncpg://moks_worker:<new>@moks-postgres:5432/moks' \
  --from-literal=MOKS_SESSION_SECRET='<same value as today>' \
  --from-literal=MOKS_CRED_<each read connector and LLM key>='<value>'

kubectl -n moks create secret generic moks-executor-secrets \
  --from-literal=MOKS_DATABASE_URL='postgresql+asyncpg://moks_executor:<new>@moks-postgres:5432/moks' \
  --from-literal=MOKS_SESSION_SECRET='<same value as today>' \
  --from-literal=MOKS_CRED_<write credentials and git token>='<value>' \
  --from-literal=MOKS_CRED_R2_ACCESS_KEY_ID='<value>' --from-literal=MOKS_CRED_R2_SECRET_ACCESS_KEY='<value>' \
  --from-literal=MOKS_CRED_R2_SSE_KEY='<value>' --from-literal=MOKS_CRED_R2_REPO_PASSWORD='<value>'
```

The API also makes interactive read-only connector calls, so it needs the same read `MOKS_CRED_*` values as the worker. It never needs write credentials.

Add to `moks-backup`: `MOKS_DATABASE_URL` for `moks_backup`, and `PGPASSWORD` with the same password. Drop the AWS keys there if the job uses an IAM role through `serviceAccount.componentAnnotations.backup`.

On EKS, prefer IAM roles for AWS access: set `serviceAccount.componentAnnotations.<component>` to separate roles rather than storing keys. RDS users need the same roles; the RDS master user can run `moks db-grants`.

## Stage 2: create the roles while everything still uses the shared Secret

Take and verify a database backup first. The next two commands only add roles and grants, and the migration 0014 trigger only acts for `moks_api`.

```sh
kubectl -n moks exec deploy/moks-api -c api -- moks migrate
kubectl -n moks exec deploy/moks-api -c api -- env MOKS_DB_PASSWORD_API='<new>' MOKS_DB_PASSWORD_WORKER='<new>' \
  MOKS_DB_PASSWORD_EXECUTOR='<new>' MOKS_DB_PASSWORD_BACKUP='<new>' moks db-grants
```

## Stage 3: switch

Set `secrets.split: true` (and `velero.enabled` if used) through a reviewed Git change, and sync. The `moks-migrate-<hash>` Job runs, runtime pods wait for it in `wait-schema`, and Deployments roll.

Check that sign-in works, that an investigation completes, and that a test plan in a non-production namespace executes. Then watch for `permission denied` in the logs.

## Roll back

Set `secrets.split: false` and sync. Components return to `moks-secrets`, which was never changed. Roles and grants stay in place and do no harm.

## Stage 4: remove what is no longer needed

After a stable period, delete `moks-secrets` and the old `moks` ServiceAccount. Rotate the schema-owner password, because every pod used to hold it, and keep it only in `moks-migrate-secrets`.
