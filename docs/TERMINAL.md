# Web terminal

A Bash shell in the portal, with AWS CLI v2, kubectl, curl, jq and DNS tools. Each session is one disposable pod in its own namespace.

A shell is arbitrary code execution inside its sandbox. MOKS does not filter commands, and aliases or wrappers could not make it read-only. **What a session can do is decided by its IAM role, its Kubernetes RBAC, Pod Security and network policy.** Changes to production stay in reviewed action plans, and an approved plan never issues shell credentials.

## How it fits together

```
browser ── wss /moks/api/terminal/... ──> moks-terminal gateway ── pods/exec ──> session pod (moks-terminals)
                                               │                                   ├─ AWS: role session, in memory
                                               └─ sts:AssumeRole                   └─ kubectl: token bound to the pod
```

- **The gateway is its own component.** The API, worker and executor gain nothing. It has its own ServiceAccount, Secret and database role, power over the terminal namespace only, and no route to the rest of the portal. It reads users, roles, sign-ins and environments, and nothing else.
- **Nothing connects to a session pod.** The shell is reached by exec through the Kubernetes API, and a pod accepts no inbound traffic.
- **tmux keeps the shell alive** when a connection drops. It is hidden: no status line, no prefix key.
- **The ingress routes `/moks/api/terminal`** to the gateway, as a longer prefix than the portal's. Nothing is stripped, and Traefik carries the WebSocket upgrade.

## Opening a session

1. The engineer chooses environment, AWS account and role, region, and optionally a cluster, and states a purpose. Nothing is preselected, and the server checks that a profile offers exactly that combination.
2. The gateway assumes the role for this session, and verifies the identity with STS. A wrong identity stops here, before any pod exists.
3. It starts the pod and writes credentials over exec's stdin into a memory-backed volume, so they appear in no pod spec, process list or database.
4. The **shell itself** runs `aws sts get-caller-identity` through the pod's own network, and must match. If a cluster was chosen, the gateway issues a token bound to the pod, reads the identity back with `kubectl auth whoami`, and refuses any identity that may create pods, read Secrets or do everything.
5. Only then does the session become ready, so input is impossible before verification.

There is no fallback. If any step fails the session fails with its reason. The pod has no service account token and `AWS_EC2_METADATA_DISABLED` is set, and network policy blocks metadata, so a shell has no other identity to reach for.

## Access

| Action | Who |
|---|---|
| Open a session | the `terminal` role, with namespace `*`, in that environment. Not included in any other role, including `admin` |
| Attach, reconnect | **the owner only**, signed in, with `terminal.use` still held. No policy shares a shell, so nobody else can attach, admins included |
| Stop | the owner, or `terminal.revoke` (admins) |
| See that a session existed | the owner, `audit.read`, `terminal.revoke` |

Someone else's session answers exactly as a missing one does. The WebSocket checks Origin before reading anything, because browsers send cookies on cross-site upgrades and a WebSocket has no CSRF token: a missing or different Origin is refused at the upgrade.

Grant it with `moks grant someone@example.com terminal:mk4:*`, or under Users & roles.

## When a session ends

| Cause | Default |
|---|---|
| No input | 15 minutes (`terminal.idleMinutes`). Only typing and paste count, so `tail -f` does not keep a session alive |
| Maximum lifetime | what was chosen, up to 60 minutes (`terminal.maxMinutes`) |
| Credentials expire | at the same moment. There is no renewal: open a new session |
| Stopped or revoked | within seconds |
| Role removed, user deactivated, or signed out of MOKS | within 15 seconds |
| `exit` in the shell | at once |

Closing the tab or losing the network does not end a session: reopen it to continue, until one of the above happens. Opening it in a second window takes it from the first.

A loop in the gateway deletes pods of ended sessions, and any pod with no live session. If the gateway is down, `activeDeadlineSeconds` still stops the pod, and a bound token dies with its pod. The AWS role session cannot be revoked by deleting the pod: it lives until it expires, at most the session's lifetime, and only a pod could use it.

## Isolation

- Non-root (UID 10002), all capabilities dropped, no privilege escalation, RuntimeDefault seccomp.
- No host namespaces or hostPath, no service account token, no Docker socket, no kubeconfig from a host.
- Read-only root filesystem. `/tmp` is a 256 MiB emptyDir, and credentials are in a 1 MiB memory volume.
- Pod Security `restricted` is enforced on the namespace, so the API server refuses anything weaker.
- CPU, memory and ephemeral storage limits on every pod, and a ResourceQuota on the namespace.

### Network

The namespace denies everything except DNS, plus what you list:

```yaml
terminal:
  network:
    apiServer: true     # for inCluster profiles: networkPolicy.apiServer.cidrs only
    egress:
      - cidr: 0.0.0.0/0
        except: [10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, 169.254.0.0/16]
        ports: [443]
```

With no `egress`, a session cannot reach AWS, and fails at step 4 with a message saying so.

**Ordinary NetworkPolicy matches addresses, not hostnames.** It cannot say "only `*.amazonaws.com`", and AWS's addresses are too many and too changeable to list. The rule above therefore allows HTTPS to the whole public internet, while the exceptions keep out private ranges (the MOKS database, every cluster service, nodes) and link-local metadata. So a session **can** send data to any public HTTPS host. To narrow that, use one of:

- A CNI with FQDN policy, such as Cilium `toFQDNs`. mk4 runs kindnet, which has none.
- An egress proxy, allowing only that in `egress`, with `HTTPS_PROXY` set in the image.
- VPC endpoints for AWS on EKS, allowing only those addresses.

DNS stays open to the cluster's resolver, which can carry small amounts of data out by design of DNS.

**A policy only works if the network plugin enforces it.** Check that once per cluster, as below.

## Credentials and permissions

**AWS.** One short-lived role session per terminal, named `moks-term-<id>`, with the person's email as `SourceIdentity`, which stays in CloudTrail. `deploy/terraform/terminal.tf` creates:

- `moks-terminal-readonly`: `ViewOnlyAccess`, plus an explicit deny on secret values, parameter values, KMS decrypt, S3 object reads, access key creation and further role assumption. Its trust policy accepts only the gateway, only session names `moks-term-*` and `moks-audit-*`, and only with a source identity.
- `moks-terminal-gateway`: a user that can do nothing except assume that role.

JSON equivalents are in `deploy/iam/terminal-*.json`. ViewOnlyAccess cannot read data, and several services' read APIs are out of its reach. Widen it with care, because whatever this role can do, every terminal user can do.

**Kubernetes, this cluster.** `moks-terminal-session` is bound to `moks-terminal-reader` only in `terminal.kubernetes.namespaces`: no Secrets, no ConfigMaps, no exec, no writes.

**Kubernetes, EKS.** The shell runs `aws eks update-kubeconfig`, so it uses the role session. Give the role an access entry with the view policy, which excludes Secrets:

```sh
aws eks create-access-entry --cluster-name prod --principal-arn <terminal_role_arn>
aws eks associate-access-policy --cluster-name prod --principal-arn <terminal_role_arn> \
  --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy --access-scope type=cluster
```

Never attach an admin policy to this role. A session refuses to start if its identity can create pods or read Secrets, but that is a safety net for mistakes, not the control.

## Audit and recording

The audit log records owner, purpose, account, role, region, cluster and namespaces, image, and each create, denial, ready, attach, detach, end and cleanup, kept like every audit record. The Terminal page can look up the role session's CloudTrail events: management events in the session's region, which can take 15 minutes to appear.

**MOKS does not record terminal input or output.** No transcript exists to protect, leak or restrict, and the page says so to the engineer. This is a decision about transcripts holding secrets, not an oversight, and it means the audit log shows who had which access and when, not what they typed.

Input recording would not be a complete command audit anyway: scripts, `eval`, pasted programs and interactive tools hide what actually runs. CloudTrail and the Kubernetes audit log are the record of what a credential did.

## Command suggestions

The engineer asks a question. The model receives that question and the session's region and cluster, **never terminal contents**, and its answer is shown with an explanation. "Put at my prompt" pastes one line, which then waits for the engineer to press Enter. A suggestion with a newline or control character is dropped, because it could run on paste. A suggestion has no permissions of its own.

## The image

`deploy/terminal/Dockerfile` pins the base by digest, Debian packages by archive snapshot, and AWS CLI and kubectl by version and SHA-256 for arm64 and amd64. kubectl 1.37 matches mk4, whose nodes are arm64. Sessions run it by digest, and the chart refuses a tag.

**To update:** change the versions, snapshot date, digest and checksums in the Dockerfile, in a pull request. Hashes come from `https://dl.k8s.io/release/<version>/bin/linux/<arch>/kubectl.sha256` and from hashing `https://awscli.amazonaws.com/awscli-exe-linux-<arch>-<version>.zip`. CI builds both architectures, runs the image read-only and unprivileged, scans it with Trivy (failing on fixable HIGH or CRITICAL findings, except those in `deploy/terminal/.trivyignore`, which expire), and runs `scripts/terminal-isolation-test.sh` on kind. After the merge, CI pushes to `moks-terminal` and reports the digest, which goes into `terminal.image.digest`. Rebuild at least monthly for security updates.

**To add a package:** the same pull request, adding it to the Dockerfile's package list. Debian's archive snapshot pins it. Anything else needs a version and checksum like AWS CLI's, from the project's own release. Review asks what it needs and what it can reach. There is no runtime installation: a session has no root, a read-only filesystem and no package manager, and that is the point.

## Enabling it

Each step here is a change to AWS or to mk4, and is yours to run.

1. **Terraform:** set `terminal_enabled = true` and apply. It also creates the `moks-terminal` repository and lets CI push there.
2. **Image:** merge, and take the digest from CI's summary.
3. **Migration:** take and verify a backup, then deploy so that migration 0023 runs. It adds `terminal_sessions` and widens the list of allowed roles.
4. **Key, database role and Secret:** run `scripts/create-terminal-secrets.sh`. It creates a key for `moks-terminal-gateway`, sets the `moks_terminal` password and grants through `moks db-grants`, and writes `moks-terminal-secrets`. Add `ROTATE=1` to replace them. Do not reuse `moks-mk4-runtime`.
5. **Pull secret:** the ecr-refresh CronJob (tools repo) creates `ecr-pull` in every namespace, but only every 6 hours. Once `moks-terminals` exists, run it once: `kubectl -n roboshop-ai create job ecr-refresh-now --from=cronjob/ecr-refresh`.
6. **Values:** set `terminal.enabled`, `terminal.image.digest`, `terminal.imagePullSecrets: [ecr-pull]`, `terminal.profiles`, `terminal.kubernetes.namespaces` and `terminal.network`.
7. **Argo:** apply `deploy/argocd/project.yaml`, then sync without prune.
8. **Grant** the `terminal` role to one person, and open a session in `moks-lab`.
9. **Verify on that cluster,** from inside the session:

```sh
MOKS_VERIFY_BLOCKED="moks-postgres.moks.svc.cluster.local:5432 moks-api.moks.svc.cluster.local:8080" moks-verify-isolation
```

Every line must read `ok`, or `skip` with a reason. **Do not open terminals to others until the metadata and database lines pass:** that is what proves mk4's network plugin enforces this policy.

## Limitations

- **Never yet run for real.** See [TESTING.md](TESTING.md#web-terminal). The image has not been built, and no pod has run on a cluster.
- **Public HTTPS and DNS are open** once AWS is allowed, as above. A session could send what it can read to any public host.
- **Read-only depends on the role.** A wrongly widened role is a wrongly widened terminal. MOKS checks Kubernetes write access at start, and cannot check IAM the same way.
- **ViewOnlyAccess is narrow.** Some read commands will be refused. That is its intent, and widening it is a decision.
- **No credential renewal.** Credentials last the session, so long work means a new session. Sessions over an hour need `MaxSessionDuration` raised on the role, and role chaining caps at an hour.
- **Files in `/tmp` go with the pod.** There is no way to move files in or out except through the clipboard.
- **Reconnecting restores the screen, not all history.** Scrollback from before a reload is gone from the browser.
- **One gateway replica by default.** A restart drops connections and the shells survive. Provisioning in progress fails and is cleaned up.
- **No shared sessions** or terminal recording, by design here.
- **Pod start needs image pull time,** up to about 3.5 minutes on a cold node before it is failed.
