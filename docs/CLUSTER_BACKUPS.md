# Cluster backups to Cloudflare R2

MOKS backs up whole clusters, Minikube or EKS, to Cloudflare R2 and restores them from the portal. Velero does the cluster work: every API object including Secrets, plus PersistentVolume contents through its node agent. MOKS is the control plane.

## What protects the data

- **MOKS never reads a Secret.** Its read role covers only Velero's status objects. Nothing in an investigation, a tool result or an LLM prompt can contain Secret data.
- **The executor can write Velero's Secrets but cannot read them.** Its role has `create` and `patch`, and no `get` or `list`.
- **Every write is an action plan.** Connecting, scheduling, backing up and restoring all use the normal RBAC, approval, hash and audit path. Only people with a binding on all namespaces can request them. Restores and schedules always need a second person.
- **R2 only ever holds encrypted data.** Objects use SSE-C with a key R2 does not keep, and volume data is also encrypted by Kopia with a repository password that MOKS sets, replacing Velero's public default.

## Keys you must keep outside the cluster

Store all four in your password manager. Without the encryption key and the repository password, backups cannot be read, by anyone.

| Variable | What it is |
|---|---|
| `MOKS_CRED_R2_ACCESS_KEY_ID` | R2 API token, Object Read & Write on this bucket |
| `MOKS_CRED_R2_SECRET_ACCESS_KEY` | Its secret |
| `MOKS_CRED_R2_SSE_KEY` | `openssl rand -base64 32` |
| `MOKS_CRED_R2_REPO_PASSWORD` | `openssl rand -base64 32`. Never change it after the first volume backup. |

## Set up

1. Create an R2 bucket and token. Add the four variables to the executor's Secret, and the first three to the API's for the storage test.
2. Install Velero in each cluster. This is a privileged change, so review it first: `helm upgrade --install velero velero --repo https://vmware-tanzu.github.io/helm-charts --version 12.2.2 -n velero --create-namespace -f deploy/velero/values-r2.yaml`
3. Set `velero.enabled: true` in the MOKS chart and sync. On an EKS cluster reached remotely, grant the same two Roles to the identities in its connector.
4. In **Admin > Backup storage**, enter the account, bucket and variable names, then run the test.
5. In **Operate > Backups**, choose **Connect to R2**, then **Set schedule**, and approve each plan.

Each cluster stores under its own prefix, `<prefix>/<environment>`.

## Restore

Choose a backup and **Restore**. The default keeps anything that exists and only creates what is missing. Overwriting is a separate choice, shown in the plan. A pre-restore backup runs first by default, and the restore does not start unless it completes. It must finish within 15 minutes, because plans run one at a time; for a larger cluster, take a backup yourself and turn this off.

Velero's namespace and MOKS's own namespace are never restored from the portal, because restoring over MOKS would stop the restore. The environment's denied namespaces are excluded too, and each plan shows the exact list.

**If the cluster running MOKS is lost,** MOKS cannot restore itself. Build the new cluster, install Velero with the same bucket and keys, and use the Velero CLI: `velero restore create --from-backup <name>`. Then restore the MOKS database from its own backup if needed.

## What AI does here

`k8s_backup_status` gives investigations Velero's status, and the `backup-health` skill reviews it. **Ask MOKS to review** on the Backups page starts one. The findings are computed by rules, and the model explains them. It cannot start a backup or restore.

## Retention

Backups default to 3650 days, so nothing is removed automatically unless you choose a shorter period for a backup or schedule. R2 bills for what is stored.

## Not yet proven

Written and checked against Velero's published schemas, but **not run against real Velero or R2**. Prove it on a scratch cluster first. Three things need checking there: R2 with `checksumAlgorithm: ""`, `customerKeyEncryptionSecret` with real backups, and Kopia volume data on R2 with SSE-C set on the location.
