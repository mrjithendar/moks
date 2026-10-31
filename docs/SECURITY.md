# Security, RBAC and policy

## Authentication

| Mode (`MOKS_AUTH_MODE`) | Use | Notes |
|---|---|---|
| `oidc` | Enterprise IdPs (Okta, Entra ID, Google, Keycloak, Dex) | Authorization code + PKCE (S256) + nonce. The ID token is verified against the issuer's JWKS (issuer, audience, expiry, nonce). `email_verified=false` is refused, and `MOKS_OIDC_ALLOWED_DOMAINS` restricts domains. Logout calls `end_session_endpoint` when the IdP has one. |
| `local` | Small installs (the mk4 default) | argon2id passwords of at least 12 characters, created by an administrator in the UI or with `moks create-user`. There is **no self-registration and no first-visitor setup page**, so a fresh install admits nobody until an operator creates the first user. |
| `dev` | Local development only | Password-less. Settings validation **refuses to start** with `dev` unless `MOKS_ENV=development`, and the endpoint returns 404 otherwise. |

New OIDC users get **no roles**. Access comes from `MOKS_BOOTSTRAP_ADMINS` (Administrator at
first sign-in), `MOKS_OIDC_GROUP_ROLES` (IdP group to grants, re-synced on every sign-in;
manual grants are kept) or an administrator.

**Sessions.** The cookie `moks_session` holds an opaque 256-bit token (`HttpOnly`, `Secure`,
`SameSite=Lax`, `Path=/moks`). Only its SHA-256 is stored. Sessions have an 8-hour idle timeout
and a 12-hour absolute lifetime, and are revoked on logout and when the user is disabled.

**CSRF.** POST, PUT, PATCH and DELETE need `X-CSRF-Token` equal to the session's token (returned
by `/auth/me`, kept in memory only) **and**, when present, an `Origin` matching
`MOKS_PUBLIC_URL`. SSE and every other GET are side-effect free.

**Rate limits** are per process: 10 logins per minute per IP and per email, 10 investigations per
minute per user, and 600 API calls per minute per session. Add a Traefik `RateLimit` middleware
for a cluster-wide limit.

**Headers.** A strict CSP (`script-src 'self'`, `frame-ancestors 'none'`), `X-Frame-Options:
DENY`, `nosniff`, `Referrer-Policy: same-origin`, and `Cache-Control: no-store` on API responses.

## Application RBAC

| Role | Permissions |
|---|---|
| viewer | `overview.read`, `investigation.read` |
| investigator | viewer + `investigation.run`, `explorer.read`, `metrics.raw_query`, `finops.read`, `incident.write` |
| operator | investigator + `action.request`, `action.execute` |
| approver | viewer + `action.approve`, `finops.read` (never their own plans; only a global admin may approve their own, and an admin's plans need no second approval) |
| admin | everything, including `admin.users`, `admin.connectors`, `admin.llm`, `admin.policies`, `audit.read` |

A binding is `role : environment-pattern : namespace-pattern` (glob). Some rules are deliberate:

- `admin.*` and `audit.read` only count from a binding whose environment **and** namespace are both `*`.
- Raw PromQL (`metrics.raw_query`) needs a namespace `*` binding, because PromQL cannot be reliably
  confined to a namespace. Namespace-scoped users get the templated, namespace-pinned
  `prom_pod_resources` instead.
- An "all namespaces" investigation can only be started or read with a namespace `*` binding,
  because it may hold evidence from any namespace.
- Investigations outside a user's scope return 404, so their existence is not revealed.

Examples:

```sh
moks grant alice@example.com investigator:mk4:*       # all namespaces in mk4
moks grant bob@example.com   investigator:prod-*:shop  # only namespace shop, any prod-* env
moks grant carol@example.com viewer                     # read everything visible
```

```bash
# OIDC group mapping (values.yaml config.oidcGroupRoles)
MOKS_OIDC_GROUP_ROLES='{"sre-admins": ["admin"], "sre-oncall": ["investigator:prod-eks:*"], "payments": ["investigator:prod-eks:payments"]}'
```

## Infrastructure scope (separate control)

Each environment has `allowed_namespaces` and `denied_namespaces` patterns that apply to
**everyone**, administrators included. The effective scope is *RBAC ∩ environment allow-list −
deny-list*. The connector's own credentials narrow it further: the chart's `moks-reader`
ClusterRole is read-only and excludes Secrets, ConfigMaps, `pods/exec`, `pods/attach` and
`pods/portforward`. On EKS, prefer `rbac.clusterWide=false` with an explicit namespace list
(see [../deploy/rbac/](../deploy/rbac/)).

Free-form LogQL from users or the LLM is parsed: every stream selector must pin
`namespace="<permitted>"` with an equality matcher. Regex matchers and unpinned selectors are
rejected unless the scope is unrestricted (`*` with no denied namespaces).

Enforcement points: the API (`require()` and `effective_scope()`), the executor (on every tool
call, including calls planned by the LLM) and the worker, which re-loads the requester's bindings
**when the job runs**. Tests cover revoking access between queueing and execution.

## LLM and prompt injection

- Evidence reaches the model redacted and wrapped in `<untrusted_data id="random">` blocks. The
  closing tag cannot be forged because the id is random per block.
- The model chooses only from read-only tools offered for the principal's scope. Unknown or
  privileged tools, invalid arguments and out-of-scope namespaces are rejected **and audited**.
  The rejection is fed back so the model does not repeat it.
- The report is validated: facts without real evidence references are dropped, causes without
  evidence fall to `low`, `verified` is always false, remediation is never executable, and
  "confirmed/resolved" wording gets an explicit "unverified" note.
- Credentials never reach prompts. Bedrock uses the pod's AWS identity, and OpenAI/Gemini keys
  stay in the provider adapter's request header.
- See `tests/test_investigations.py::test_prompt_injection_cannot_widen_tools_or_scope`.

## Revoking access during running work

Roles are not trusted from when work started. Triggers maintain `users.auth_version` and `environments.policy_version`, so no write path can forget them. A running investigation compares them before every tool call, before every LLM request, and every 5 seconds. Open event streams recheck the session and role every 5 seconds, then send `revoked` and close.

**Maximum delay:** 5 seconds for running work, and none for a new tool call or LLM request. Conversation history sent to a model leaves out turns the person can no longer read.

Revocation cannot recall what has left: a prompt already sent to an LLM provider, or a query already at Prometheus, stays sent. A revoked investigation stores no partial report.

## LLM budgets

Every call, including embeddings, reserves its worst-case cost before it runs (`moks/llm/budget.py`), inside a lock per provider. The day is the UTC calendar day. A budget of 0 stops paid use; free models still run until any spend exists. Each provider also has `max_concurrency`, and `MOKS_LLM_USER_DAILY_BUDGET_USD` and `MOKS_LLM_INVESTIGATION_BUDGET_USD` add limits per person and per investigation. A call with an unknown outcome stays counted at its estimate for that day. `llm_usage` records estimated and actual cost.

## Secrets

- Connector and LLM rows store **references**: `{"type":"env","name":"MOKS_CRED_..."}` (only that
  prefix is readable) or `{"type":"file","path":"..."}` under `MOKS_CREDENTIALS_DIR`, with path
  traversal blocked. The admin API refuses config keys that look like credentials.
- The values live in Kubernetes Secrets created out of band ([../deploy/secrets/](../deploy/secrets/)).
  Argo CD's `moks` AppProject cannot create or prune Secrets. Use External Secrets Operator or the
  Secrets Store CSI driver for an external secret manager; the references stay the same.
- Redaction (`moks/redaction.py`) masks bearer/basic tokens, JWTs, AWS keys, provider API keys,
  credentials in URLs, PEM keys and sensitive `key=value` pairs, plus literal values of env vars
  with sensitive names in pod specs. It runs on evidence, prompts, audit details and API
  responses. It is pattern-based, so treat it as a safety net.
- Nothing sensitive goes to browser storage. The SPA keeps the CSRF token in memory, and
  `localStorage` is not used.

## Audit

`audit_events` records sign-ins (including failures), authorization denials, every tool call
(allowed and denied, with arguments and evidence reference), investigation lifecycle and every
configuration change. Integrity has three layers:

1. **Append-only triggers.** UPDATE, DELETE and TRUNCATE raise errors.
2. **Hash chain.** A BEFORE INSERT trigger, serialised by an advisory lock, sets `chain_seq`,
   `prev_hash` and `hash = sha256(prev_hash | seq | row content)`. `moks verify-audit` and
   *Audit log → Verify chain* recompute it.
3. **Tamper evidence, not tamper proofing.** A database superuser (or the `moks` owner role) can
   disable triggers and rewrite the whole chain consistently. The test suite shows that edits
   without a rewrite are detected.

For stronger guarantees, ship records off-cluster where MOKS's credentials cannot change them:

- Periodically export new rows (`SELECT ... WHERE chain_seq > last`) to S3 with Object Lock in
  compliance mode, or to a separate logging account. Then keep only the latest `hash` checkpoint
  somewhere independent. Any later rewrite shows up as a mismatch against the archived checkpoint.
- Alternatively, also emit the `audit` JSON log lines (already on stdout) to a write-once log store.

## Network policies

The chart allows: ingress to `:8080` from the `traefik` namespace and to `:9464` from
`monitoring`; egress to DNS, PostgreSQL, the Kubernetes API endpoints (listed explicitly),
Prometheus/Loki/OTel in-cluster, and HTTPS to public addresses (LLM providers, OIDC issuer, AWS).
PostgreSQL accepts only MOKS pods and has no egress.


## Remediation (Phase 3)

- **Separate identity.** Only the `moks-executor` Deployment holds write credentials: its own
  ServiceAccount, bound per namespace (`executor.namespaces`) to `deployments` get/update/patch,
  `replicasets` list, `services`/`horizontalpodautoscalers` patch, and read verbs for validation.
  The API and worker cannot even construct the writer (`MOKS_PROCESS_ROLE` guard).
- **Allowlist.** `restart_deployment`, `scale_deployment` (within `max_replicas`; zero only if
  allowed), `rollback_deployment` (to a revision still in history), `patch_resources`,
  `apply_config_patch` (JSON merge patch on Deployment/Service/HPA; fields such as `securityContext`,
  `hostNetwork`, `hostPath`, `image`, `command`, `serviceAccountName` and `selector` are refused),
  `open_pull_request` (draft PR). Anything else cannot be planned.
- **Binding.** The plan hash covers environment, namespace, action, target, parameters, requester,
  expiry and the target's `resourceVersion`. Approvers submit the hash they reviewed. Approvals by
  the requester are refused. Approvals bound to another hash, from someone who has since lost
  `action.approve`, or for an expired plan are ignored at execution.
- **Execution.** The executor re-verifies everything, requires the live `resourceVersion` to equal
  the reviewed one, executes once (status transitions under row locks), validates the rollout and
  pod health, and restores the captured pre-state if validation fails. Every step is audited and
  added to the incident timeline.
- **Production.** Every action in a production environment needs approval. Outside production,
  restart, scale and resources changes can be executed by an operator directly; rollback, config
  patches and PRs always need approval.

## Diagnostic sessions

Session pods run in `moks-diagnostics` with no ServiceAccount token, `activeDeadlineSeconds`, a
ResourceQuota and LimitRange, and a NetworkPolicy allowing DNS and egress except link-local/metadata
and the Kubernetes API. They run non-root with all capabilities dropped. **Elevated** sessions run
as root inside the pod, for catalog OS packages. No session needs an approval, in any environment:
it starts at once for anyone who may execute in the diagnostics namespace, and is audited. Commands are
argv templates from the catalog with validated arguments (hosts, URLs, ports, jq paths). There is
no shell, metadata and loopback targets are refused, and none of it is exposed to the LLM. Output
is redacted and audited, and pods are deleted at TTL or termination.

## Alerts

The webhook requires `Authorization: Bearer $MOKS_ALERT_WEBHOOK_TOKEN` (disabled when unset) and is
rate-limited. Automatic investigations run as `automation@moks.system`, which has bindings only in
environments with `auto_investigate` enabled. Environment deny lists still apply, and investigations
are capped at 6 per environment per hour.

## Knowledge and skills

Documents keep the scope of their source (environment/namespace). Retrieval filters in SQL and
again on every result. Secret files are never stored (`.env`, keys, `*.pem`, kubeconfigs,
Kubernetes Secret manifests with data). Everything else is redacted before chunking. Retrieved text
reaches the model only as untrusted data, and a test shows a runbook that says "scale to zero"
cannot cause an action. **Skills** are the opposite: trusted, reviewed instructions from the image
or a ConfigMap, validated at load to use only read-only tools and allowlisted action hints
([SKILLS.md](SKILLS.md)).

## LogQL scope and AI query drafts

Scope is checked on tokens, not with a pattern over the text. `moks/connectors/loki.py` reads strings, `#` comments and
selectors the way Loki's lexer does, so a comment cannot hide a selector. For anyone without every namespace, each selector
must pin the namespace with `=`, comments are refused, and a value with an escape does not count as pinned. Ranges and
`offset` cannot look back further than the environment's query window. This runs on every execution, whoever wrote the query.

"Generate LogQL with AI" only fills the editor. It checks access first, then sends the model the request and a bounded list
of label names and plain-token values for that scope. It never sends credentials or log lines. A draft that leaves the chosen
namespace, contains a comment, looks back too far or fails Loki's parser is discarded and the reason is shown. Log search text
and LogQL go in request bodies, not URLs.

## What stays in the browser

Conversation drafts, unsent LogQL and log search text are kept in `sessionStorage` under `moks.`. They end with the tab, and
are cleared on sign-out and when a session expires. Shareable filter state in the URL never includes search text or IDs.

## Administrator approval

A global administrator (a role binding on every environment and namespace) can approve their own plans, and plans they
request need no second approval, including in production. This removes separation of duties for that role: one
administrator account can change production alone. Plans keep their hash, expiry, single execution and explicit Execute step.
Each waiver is recorded as `action.approval_waived`, and the executor refuses the plan if the requester is no longer an administrator.

### Work that needs no approval

Diagnostic sessions, cluster backups, backup schedules, the R2 connection and database backups need no approval, including in
production. They still need the requester's RBAC and are audited. Restores still need one. The executor can run `pods/exec`
in `databaseBackups.namespaces`, limited in the code to the fixed dump commands, which is a wider permission than it had before.

### Force approve

A global administrator can also force approve someone else's pending plan, or an expired one, with a written reason of at
least 10 characters (`POST /actions/plans/{id}/force-approve`, recorded as `action.force_approve`, refused attempts included). An
expired plan gets a new expiry window, and because the expiry is hashed, a new plan hash; the audit entry keeps the previous status,
hash and expiry.

Force approve replaces only the approval. It does not change the plan's content, and rejected, cancelled and finished plans
stay closed. Execute remains a separate step, and the executor still checks the hash, current policy, the requester and the write
target. It also refuses the plan if the object's `resourceVersion` changed since the plan was written, so a stale plan stops rather
than overwriting newer state. A forced approval stops counting when its author is no longer a global administrator.
