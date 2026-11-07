# Secret sync: Kubernetes ⇄ AWS Secrets Manager

MOKS can list Kubernetes Secrets and AWS Secrets Manager secrets, compare mapped pairs, and copy content in
either direction when a person asks and another person approves. It never syncs on a schedule and never
propagates a change by itself. Only discovery runs on its own: the executor lists the `secretsSync.namespaces`
and each AWS connector when it starts and every `secretsSync.discoveryIntervalMinutes` (default 60; 0 turns it
off). Discovery reads metadata and never writes, so new and deleted secrets show up without anyone pressing
Discover. Changed content still shows up only when someone compares.

Every write follows **discover → select → compare → review → approve → execute → verify**.

The feature is off until `secretsSync.enabled: true` in the chart (`MOKS_SECRETS_SYNC_ENABLED`).

## Where values go, and where they never go

| Place | Holds |
|---|---|
| Executor process memory, during one job | Secret values, briefly |
| PostgreSQL (`secret_*` tables) | Names, types, key names, owners, version ids, ARNs, and keyed digests |
| Job payloads and results | Plan and mapping ids, counts |
| Audit events | Actor, scope, action, key names, version ids, approval and outcome |
| Logs and traces | Event names, namespaces, regions. No names of keys, no values |
| API responses and the browser | Names, key names (with `secrets.compare`), masked fingerprints. Values only as a reveal, sealed to one browser tab (below) |
| LLM prompts, investigation tools, CrewAI | Nothing. `moks.secrets` is not a tool and no agent can call it |

Values are read and written only by the executor (`MOKS_PROCESS_ROLE=executor`), the one component that already
holds write credentials. The API has no Kubernetes permission on Secrets and no Secrets Manager permission, and it
calls neither. Discovery, comparison and execution run as jobs of class `secrets`, which only the executor claims.

**Keyed digests.** Each key's value is compared by HMAC-SHA256 under `MOKS_SECRETS_DIGEST_KEY`, which only the
executor has. A stored digest of a short password therefore cannot be brute-forced from the database. The review
shows the first 8 characters of that digest (a fingerprint), plus whether the value is empty or binary. It never
shows a length or a value. If the key is rotated, old baselines no longer apply: the next comparison asks for an
explicit source.

**Discovery and values.** The Kubernetes API has no call that returns key names without values, so a listing
carries values through the executor. They are dropped in the same function, and so are all annotations,
because `kubectl.kubernetes.io/last-applied-configuration` holds a clear-text copy of a Secret's data. AWS
discovery is metadata only (`ListSecrets`). AWS key names appear after the first comparison.

**Viewing values.** People with `secrets.reveal` (the `secrets_approver` role, and admins) open a secret's details
on the Inventory tab and choose Show values. The tab makes an ECDH P-256 key pair whose private half cannot be
exported, and sends only the public half. The executor reads the secret in a `secret_reveal` job and encrypts it
with AES-256-GCM, under a key derived with HKDF-SHA256 from the ECDH secret and the job id
(`backend/moks/secrets/seal.py`, `frontend/src/secretReveal.ts`). PostgreSQL and the API hold only that
ciphertext, which nobody but that tab can open. The API hands it over once, to the person who asked, then removes
it, and the executor clears any result left after 5 minutes. The page masks each value until it is asked for and
hides them all after 2 minutes. Both the request and the read are audited (`secrets.reveal_request`,
`secrets.reveal`) with key names, never values. A Kubernetes Secret needs `secrets.reveal` in its namespace. An AWS
secret needs it in the namespace it is mapped to, or for every namespace when it is unmapped. Values over 256 KiB
are not shown.

`tests/test_secret_sync.py::test_secret_values_stay_out_of_storage_logs_traces_errors_and_responses` runs whole
flows, including failures, with marker values. It then searches every table, all captured logs, every span and
every API response for the value, its base64 form and its plain SHA-256, and finds none of them.

## Concepts

**Mapping.** One Kubernetes Secret (cluster UID, namespace, name) paired with one AWS secret (account, region,
name). A mapping also sets:

- the payload format
- the Kubernetes type
- the direction: `export` (Kubernetes → AWS), `import` (AWS → Kubernetes) or `both`
- the expected owner on each side
- a management policy

At most one active mapping may use a given Kubernetes Secret, and at most one a given AWS secret. Mappings carry
a `version`, and an edit must present the version it was based on (optimistic concurrency).

The default AWS name is `moks/{environment}/{cluster}/{namespace}/{name}`, so the same Secret name in two clusters
or two namespaces never collides. The template is set per connector. You review and can edit proposed names
before saving, and nothing is created in AWS until a plan runs.

**Payload formats on AWS.**

| Format | AWS value | Carries |
|---|---|---|
| `moks-k8s-v1` (default) | `{"format":"moks.k8s-secret/v1","type":…,"data":{key: base64}}` in SecretString | Everything: any bytes, empty values, absent vs empty, the Secret type |
| `json` | `{"aws_key":"text"}` in SecretString | UTF-8 text values. `key_mapping.keys` `{aws_key: k8s_key}` selects and renames keys. On export, AWS keys outside the list are kept |
| `text` | The whole SecretString | One key (`key_mapping.key`), UTF-8 |
| `binary` | SecretBinary | One key (`key_mapping.key`) |

Anything a format cannot carry without loss is refused and named: binary in `json`, a non-string JSON value,
extra keys for `text`, or a value over 64 KiB. MOKS never approximates. Importing existing AWS secrets uses
`json`, `text` or `binary` with explicit key mapping.

**Baseline.** The last state both sides were verified to agree on: per-key digests, the Kubernetes UID and
resourceVersion, and the AWS version id. It moves only after a sync was read back from the destination. It also
moves when a comparison observes both sides equal, because that is an agreement too. Every change to it uses a
compare-and-set on the mapping's version and `baseline_seq`.

## Comparison

Each mapping is compared three ways: Kubernetes, AWS, and the baseline.

| Classification | Meaning | Proposed |
|---|---|---|
| In sync | Same content | Nothing |
| Changed in Kubernetes only | AWS still matches the baseline | Kubernetes → AWS |
| Changed in AWS only | Kubernetes still matches the baseline | AWS → Kubernetes |
| Changed on both sides | Both moved, or first sync with different content | **Nothing: the reviewer chooses** Kubernetes, AWS, or per key |
| Missing in Kubernetes / in AWS | One side does not exist | Recreate from the other side (reviewed) |
| Unmanaged or unsupported | Excluded type, format mismatch, scheduled AWS deletion | Nothing |
| Access denied or comparison failed | RBAC, IAM, KMS, identity or connector problem; the reason is shown | Nothing |

Timestamps are shown but never pick a winner. A per-key ("edited") resolution chooses Kubernetes, AWS or *omit*
for every key in either side. The result can be worked out from digests alone, so the API computes it without
seeing a value, and the executor checks the real content against it before writing. That can mean writing both
sides.

## Ownership

Discovery reads who manages a secret:

- **Kubernetes:** External Secrets, the Secrets Store CSI driver, cert-manager, Helm, Argo CD, Flux, Sealed
  Secrets, or MOKS itself.
- **AWS:** an owning AWS service (for example RDS), a rotation function, or a `managed-by` tag.

A managed secret can be a sync *source*. MOKS writes *into* it only when the mapping's management policy is
`override` and its expected owner names that manager. Even then the review warns that the controller may revert
the change.

Service-account tokens, bootstrap tokens and Helm release records (`helm.sh/release.v1`) are never offered for
mapping.

## Review, approval, execution

The review shows, per item:

- source and destination: account, region, cluster and namespace
- the action: create, update, make a version current, or delete
- added, removed and changed key names, with masked fingerprints
- type changes, and why a change is blocked (for example, Kubernetes cannot change a Secret's type in place)
- ownership warnings, and the conflict resolution
- workloads that read the Secret, and how

On workload impact: environment variables (`env`, `envFrom`) and `subPath` mounts need a restart. Mounted
volumes refresh within about a minute, but the application must reread the file. `imagePullSecrets` matter only
for the next pull. **Nothing restarts as part of a sync.** "Propose restart" creates an ordinary
`restart_deployment` action plan, which goes through the action approvals.

Approval is bound to a plan hash. The hash covers every item's inclusion, mapping version, baseline sequence,
source and destination versions and digests, resolution, actions, expected digest and changed key names, plus the
requester and expiry. The rules match action plans: never your own plan, unless you are a global administrator,
whose sync plans skip the second approval (audited as `secrets.approval_waived`). **Deletion plans always need
someone else's approval**, and the approver types back a confirmation phrase.

Before writing each item, the executor:

1. Takes a lease on the mapping, so MOKS runs one operation per mapping at a time.
2. Re-checks the plan hash, expiry, and the requester's, executor's and approver's current permissions.
3. Requires the mapping version and baseline sequence to be the ones compared.
4. Reads both sides again. Anything that moved makes the item **stale**: it needs a refreshed plan and a new
   approval, never a retry.
5. Rebuilds the content from the sources and requires the approved digest.
6. Skips a destination that already holds that content, so no redundant AWS version is written.
7. **Kubernetes:** `replace` with the approved `resourceVersion` (409 if anything changed) or `create`; deletes
   carry uid and resourceVersion preconditions. Labels, annotations and owner references are kept.
8. **AWS:** `PutSecretValue` with `ClientRequestToken` = the item's token and only the label `MOKSPENDING`. Then
   `UpdateSecretVersionStage` moves `AWSCURRENT` to it with `RemoveFromVersionId` = the approved version, and
   Secrets Manager refuses that move if `AWSCURRENT` has moved meanwhile. `AWSPREVIOUS` follows automatically.
   The secret's KMS key and tags are never touched. New secrets get the connector's `kms_key_id` (or the AWS
   managed key), the required tags, and `moks:managed-by`/`moks:mapping`.
9. Records the intent before each call. After a timeout or lost connection the outcome is unknown, and a retry
   **reads what landed** first: the same token returns the same version, and a destination that already holds
   the content is left alone.
10. Reads the destination back and requires the approved digest. Only then does the baseline move.

Each item is tracked as pending, running, succeeded, failed, stale, skipped or conflict. A partially completed
plan stays reviewable. Failed items can be retried under the same approval while it is valid. Succeeded items
are verified and their baselines moved. If the executor stops mid-plan, its next loop marks the running items
failed ("outcome not known"). They keep their write state, so a retry resolves them.

### The race window that remains

Kubernetes writes are fully conditional. AWS has no precondition on `PutSecretValue`, so a client outside MOKS
can write between MOKS's staged put and its conditional label move. The move then fails, the item goes stale,
the outside write stays current, and MOKS's staged version loses its `MOKSPENDING` label (Secrets Manager
deprecates unlabelled versions). What MOKS cannot prevent: an outside client that writes *after* the move and
before MOKS reads back. Verification then fails and the baseline does not move. MOKS's own operations on one
mapping are serialized by the lease.

## Deletion, recreation, rollback

- **Missing resources are reported, not propagated.** If one side disappears, the comparison says "Missing in
  Kubernetes/AWS" and proposes a reviewed recreation from the other side.
- **Deletion** is a separate plan kind (`Propose deletion`) that names its targets. It needs a second person and
  the confirmation phrase.
  - AWS deletion also needs `allow_delete: true` on the connector. It uses `DeleteSecret` with the connector's
    `recovery_window_days` (7–30) and never `ForceDeleteWithoutRecovery`. Until the window ends, the secret can
    be brought back with `aws secretsmanager restore-secret`. While it is scheduled for deletion, MOKS reports
    it as unsupported and will not sync into it.
  - Kubernetes deletion also needs `secretsSync.allowDelete: true` in the chart, which adds the `delete` verb.
- **Rollback** starts from History ("Propose rollback to the previous AWS version"). A rollback plan makes an
  earlier AWS version current again by moving the label conditionally; no new version is written. It can also
  write that version's content to Kubernetes. It is compared, reviewed and approved like any plan.
  - Kubernetes keeps no history, and MOKS stores no values. So the only recovery source is AWS's own version
    history (`AWSPREVIOUS` or an explicit version id). No other protected recovery source is configured.

## Permissions

| Permission | `secrets_viewer` | `secrets_operator` | `secrets_approver` | admin |
|---|---|---|---|---|
| `secrets.discover`: names, owners, status | ✓ | ✓ | ✓ | ✓ |
| `secrets.compare`: key names, masked differences | ✓ | ✓ | ✓ | ✓ |
| `secrets.map`: create, edit, retire mappings |  | ✓ |  | ✓ |
| `secrets.propose`: sync, rollback, deletion plans |  | ✓ |  | ✓ |
| `secrets.approve` |  |  | ✓ | ✓ |
| `secrets.execute`: run and retry |  | ✓ |  | ✓ |
| `secrets.reveal`: view values (sealed, audited) |  |  | ✓ | ✓ |

These roles are not part of `operator` or `approver`. A binding's environment and namespace patterns scope them,
intersected with the environment's allowed and denied namespaces (as for every MOKS permission).

Listing a whole AWS account needs `secrets.discover` for every namespace. A namespace-scoped person sees only the
AWS secrets mapped into their namespaces.

On top of MOKS RBAC, every operation stays inside the connector's boundaries, checked in the executor:

- the AWS account, verified with `sts:GetCallerIdentity` on each client build
- the region
- `allowed_prefixes`, matched case-sensitively, although AWS's own name filter is not case-sensitive
- `required_tags`
- `kubernetes_namespaces`
- the cluster UID recorded at discovery: a connector repointed at another cluster fails the comparison

Then the cluster's own RBAC, IAM and KMS apply.

Restarts proposed from a review need `action.request`, as for any action.

Audit events: `secrets.discover_request`, `secrets.discover`, `secrets.mapping_create|update|retire`,
`secrets.plan_create|edit|submit|cancel`, `secrets.compare`, `secrets.approve|reject|approval_waived`,
`secrets.execute_request`, `secrets.item_execute`, `secrets.execute` and `secrets.retry`; for schedules
`secrets.schedule_create|edit|approve|reject|pause|resume|retire|run|run_now`; for values `secrets.reveal_request`
and `secrets.reveal`. They carry names, key
names and version ids, never values, digests or request bodies.

## Mapping many secrets at once

Select secrets on the Inventory tab (or Select all unmapped), then Map. Each one gets the mapping Map would
propose: the name template, the moks-k8s-v1 format and the Secret's type. Choose the direction (sync to AWS, both
ways, or import). For AWS secrets, also choose the cluster and the namespace to import into; the Kubernetes name is
the last part of the AWS name. Secrets that are excluded, already mapped, outside the connector, or named so that
they look like an ARN suffix are skipped, and the reason is shown. Optionally this also starts one comparison for all
of them, and creates a schedule that waits for approval. Review each one instead keeps the per-secret editor.

## Sync schedules

Select mappings on the Mappings tab, then Schedule sync, and give a name and an interval (5 minutes to 7 days).
A schedule runs nothing until it is approved, by someone else or, for an administrator's own schedule, by the
administrator. The approval binds the schedule's hash: the environment, the creator, the interval and each mapping
at its current version. Editing the interval or the mapping set, or editing one of its mappings, needs a new
approval. Until then the schedule pauses with the reason and lists the edited mappings; its creator chooses
Refresh to ask again.

Each run is a normal sync plan, requested by the schedule's creator and marked with the schedule's name. It
compares every mapping, then writes only one-sided changes in each mapping's direction: changed on one side since
the last verified sync, or missing on one side. Left for a person, and listed on the schedule with the reason:

- changes on both sides, and a first sync where the sides differ
- secrets deleted and recreated since the last sync
- anything blocked (direction, owner, format, type, immutability)
- failures

A run never deletes. Before writing, the executor checks everything again: the schedule's hash, the approval, and
that the approver still has `secrets.approve` and the creator still has propose, compare and execute rights. If
any check fails, the schedule pauses. Run now moves the next run to now. Pause and Resume are for the creator or
an approver. Resuming checks the approval first.

The executor creates and queues scheduled plans itself, under column grants (`backend/moks/db_roles.py`). It may
hash and queue a plan, but it cannot waive approval or record one: it has no `requires_approval` grant and no
insert on approvals. Only the API inserts schedule approvals, and they cannot be changed afterwards.

## Configuration

### 1. Chart

```yaml
secretsSync:
  enabled: true
  namespaces: [shop, payments]   # each gets Role moks-secret-sync for moks-executor; must exist
  allowDelete: false             # true adds the delete verb, for reviewed deletion plans
  executorSecret: moks-secret-sync
  discoveryIntervalMinutes: 60   # the executor lists `namespaces` on its own this often, and at start; 0 = on request only
```

`namespaces` is the one list of namespaces: the executor gets a Role in each, discovery covers each, and Discover
with nothing ticked means all of them. On Argo CD each one also needs a destination in the environment's
`deploy/argocd/<env>/project.yaml`, or the executor's sync fails as a whole. Leave `argocd` out: write access to
its Secrets is write access to Argo CD's cluster and repository credentials.

The Role grants `get, list, create, update` on Secrets (plus `delete` with `allowDelete`), and `list` on
Deployments, StatefulSets, DaemonSets and Pods for the impact review. It goes to `moks-executor` only. The
read-only `moks-reader` ClusterRole still has no Secret access.

### 2. The executor's Secret

`moks-secret-sync` is loaded by the executor container alone, whether or not `secrets.split` is on. It holds:

- `MOKS_SECRETS_DIGEST_KEY`: 32+ random characters. Keep it stable: rotating it means every mapping needs an
  explicit source on its next sync.
- On Minikube: `MOKS_CRED_SECRETS_AWS_ACCESS_KEY_ID` and `MOKS_CRED_SECRETS_AWS_SECRET_ACCESS_KEY`.

`scripts/create-secret-sync-secret.sh` creates it. It prompts for the keys without echoing them and generates the
digest key. Restart the executor afterwards.

### 3. AWS identity

- **EKS:** give `moks-executor` a Pod Identity association (or IRSA) to a role with the policy in
  [examples/secrets-sync/iam-policy.json](examples/secrets-sync/iam-policy.json). Leave `credentials` out of the
  connector config.
- **Minikube:** an IAM user (or a role it assumes) with the same policy, with its keys in `moks-secret-sync`,
  referenced from the connector:
  `"credentials": {"access_key_id": {"type": "env", "name": "MOKS_CRED_SECRETS_AWS_ACCESS_KEY_ID"}, "secret_access_key": {"type": "env", "name": "MOKS_CRED_SECRETS_AWS_SECRET_ACCESS_KEY"}}`.
- **Another account:** set `role_arn` (and `external_id`) on the connector. The role must be in `account_id`.
  See [examples/secrets-sync/cross-account-trust.json](examples/secrets-sync/cross-account-trust.json).

### 4. The connector

Admin → Connectors → AWS Secrets Manager, or seed:

```json
{"type": "aws_secrets", "name": "aws-secrets", "config": {
  "account_id": "123456789012", "region": "eu-west-1",
  "allowed_prefixes": ["moks/"], "required_tags": {"team": "platform"},
  "kubernetes_namespaces": ["shop"],
  "kms_key_id": "alias/moks-secrets", "allow_delete": false, "recovery_window_days": 30,
  "name_template": "moks/{environment}/{cluster}/{namespace}/{name}"}}
```

`kubernetes_namespaces` is optional. Left out, the connector covers every `secretsSync.namespaces` entry; a list
narrows that, and never widens it.

The Kubernetes connector needs a write target (`config.executor`), because the executor reads and writes Secrets
through it.

### 5. Roles

Grant `secrets_operator` and `secrets_approver` to different people, scoped to an environment and namespace.

## Least privilege, by feature

[examples/secrets-sync/iam-policy.json](examples/secrets-sync/iam-policy.json) has one statement per feature;
drop the ones you do not enable.

| Feature | Secrets Manager | KMS (customer key) |
|---|---|---|
| Discovery | `ListSecrets` (cannot be scoped to a resource), `DescribeSecret` on the prefix | — |
| Comparison, import | `GetSecretValue`, `ListSecretVersionIds` on the prefix | `kms:Decrypt` via Secrets Manager |
| Export and rollback | `PutSecretValue`, `UpdateSecretVersionStage` on the prefix | `kms:GenerateDataKey`, `kms:Decrypt` |
| Creating AWS secrets | `CreateSecret`, `TagResource`, only with the `moks:managed-by=moks` request tag | as export |
| Deletion | `DeleteSecret` with `secretsmanager:RecoveryWindowInDays >= 7` and no force deletion | — |

With the AWS managed key (`aws/secretsmanager`), no KMS statement is needed. With a customer key, add
[examples/secrets-sync/kms-key-policy.json](examples/secrets-sync/kms-key-policy.json) to the key policy. A KMS
denial shows as "KMS refused …" on the item, distinct from an IAM denial.

Kubernetes RBAC for the executor is the Role above. The RBAC example for clusters managed outside this chart is
[examples/secrets-sync/rbac.yaml](examples/secrets-sync/rbac.yaml).

## Minikube smoke test with disposable secrets

Everything here is throwaway: one namespace, one Secret, AWS names under `moks-smoke/`. Each step that writes
to the cluster or to AWS is marked; run those yourself.

1. **(cluster write)** Create the namespace and a Secret with an empty value and a binary value:
   ```bash
   kubectl --context mk4 create namespace moks-secret-smoke
   head -c 16 /dev/urandom > /tmp/smoke.bin
   kubectl --context mk4 -n moks-secret-smoke create secret generic smoke \
     --from-literal=user=smoke-user --from-literal=empty= --from-file=blob=/tmp/smoke.bin
   rm /tmp/smoke.bin
   ```
2. **(cluster write)** Deploy with `secretsSync.enabled=true` and `secretsSync.namespaces=[moks-secret-smoke]`.
   Run `scripts/create-secret-sync-secret.sh` with an IAM user limited to `moks-smoke/*`, then
   `kubectl --context mk4 -n moks rollout restart deploy/moks-executor`.
3. Add an `aws_secrets` connector with `allowed_prefixes: ["moks-smoke/"]`,
   `kubernetes_namespaces: ["moks-secret-smoke"]` and `allow_delete: true`, and grant yourself
   `secrets_operator` and a second account `secrets_approver`.
4. Secrets sync → Discover → choose `moks-secret-smoke` and AWS. The inventory should show `smoke` with keys
   `blob, empty, user` and status *Not mapped*.
5. Select it → Map → set the AWS name to `moks-smoke/smoke`, format `moks-k8s-v1`, direction *Both* → Create.
6. Mappings → Compare. You should see *Missing in AWS* and **Create in AWS**, with three added keys and `empty`
   shown as empty → Submit.
7. As the approver: Approve. As the operator: Execute. The item should be *succeeded*, and the mapping *In sync*.
8. **(AWS read)** Check:
   `aws secretsmanager get-secret-value --secret-id moks-smoke/smoke --query SecretString --output text | jq '.data | keys'`
   should print `["blob","empty","user"]`.
9. **(AWS write)** Change it outside MOKS:
   ```bash
   aws secretsmanager put-secret-value --secret-id moks-smoke/smoke --secret-string \
     "$(aws secretsmanager get-secret-value --secret-id moks-smoke/smoke --query SecretString --output text \
        | jq -c '.data.user = ("rotated" | @base64)')"
   ```
   Compare again: *Changed in AWS only*, update Kubernetes, `user` changed. Approve and execute, then check:
   `kubectl -n moks-secret-smoke get secret smoke -o jsonpath='{.data.user}' | base64 -d` prints `rotated`.
10. Compare once more: *In sync*, and Submit refuses with "nothing to do". `aws secretsmanager list-secret-version-ids --secret-id moks-smoke/smoke` shows no new version.
11. History → *Propose rollback* → approve → execute. AWSCURRENT goes back to the first version, and Kubernetes
    `user` to `smoke-user`.
12. **(cluster and AWS writes) Clean up:** retire the mapping, then
    `aws secretsmanager delete-secret --secret-id moks-smoke/smoke --force-delete-without-recovery` (this is a
    disposable secret, so skipping the recovery window is fine) and
    `kubectl --context mk4 delete namespace moks-secret-smoke`.

## Limits

- One mapping per Secret and per AWS secret. No fan-out of one source to many destinations.
- Kubernetes cannot change a Secret's type in place, or update an immutable Secret. Such items are blocked with a
  pointer to deleting and recreating them.
- AWS: 64 KiB per value, and `moks-k8s-v1` base64 adds about a third. Larger Secrets are refused.
- Replica regions of an AWS secret are not managed; Secrets Manager replicates the primary itself.
- Comparison reads each mapping sequentially in one executor job, and plans hold at most
  `secretsSync.maxPlanItems` (100) mappings.
