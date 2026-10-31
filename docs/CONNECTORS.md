# Connectors and tool contracts

Connectors are configured per environment in **Administration → Connectors** (or seeded from
Helm values). A row holds non-secret JSON config and a credential *reference*. **Test** runs a
real request and stores the outcome, which the Overview shows. Only the first enabled connector
of each type per environment is used.

## Status

| Connector | State |
|---|---|
| Kubernetes (`in_cluster`, `kubeconfig`, `eks`) | Implemented. In-cluster and kubeconfig verified live against mk4; `eks` mode generates presigned-STS tokens (unit-tested; needs an EKS cluster to verify live) |
| Prometheus, Loki | Implemented, verified live against mk4 |
| Grafana | Implemented; reaches mk4 Grafana 13.2.4 but needs a service-account token (`auth: 401` until configured) |
| CloudWatch | Implemented; tested against botocore's Stubber. Needs credentials and log groups to verify live |
| Git (GitHub) | Implemented; ingestion verified live against the public `mrjithendar/moks` repository |
| AWS cost (Cost Explorer, anomalies, budgets) | Implemented; tested against the Stubber. Needs `finops_enabled` in Terraform to verify live |
| Cost model | Implemented; verified live (allocation and rightsizing of `roboshop-ai`) |

## Common behaviour

- **Timeouts:** per connector (`timeout_seconds`), with a hard per-tool-call ceiling of 45 s
  inside investigations and 30 s for interactive reads.
- **Retries:** idempotent reads retry twice on timeouts, connection errors and 5xx, with backoff
  of 0.3 s and 0.9 s. A 429 is not retried, because repeating a rate-limited request extends the
  block: the error carries the wait, and jobs come back after it. Other 4xx are not retried.
- **Rotation:** a mounted credential file is re-read when it changes. A value in an environment
  variable changes with the pod. Old clients close two minutes later, so calls in flight finish.
- **Size:** responses over 8 MB are refused (`too_large`). Results are redacted, then truncated
  structurally to `MOKS_MAX_RESULT_BYTES` (48 KB) with `truncated: true`.
- **Pagination:** Kubernetes lists page 250 items at a time up to 1,000 per call. Prometheus
  results cap at 25 series × 240 points (downsampled). Loki returns at most 500 lines of
  2,000 characters each.
- **Errors** carry a kind and a message the user can act on: `config` (bad setup or missing
  credential), `auth`, `forbidden` (credential lacks RBAC), `unavailable`, `timeout`,
  `bad_request` (with the backend's own error, such as a PromQL parse error, or where a redirect leads), `not_found` (with a
  base-URL/path-prefix hint) and `too_large`.

## Kubernetes

```json
{"mode": "in_cluster", "timeout_seconds": 15}
{"mode": "kubeconfig", "context": "mk4"}   + credential_ref {"type": "file", "path": "kubeconfig"}
```

Read-only by construction: the class has no write methods and no Secret reads. API discovery
(`ApisApi.get_api_versions`) runs before optional resources are queried. Pod summaries include
waiting and terminated reasons, last-termination time, exit code and owner. Env values with
sensitive names are masked. Detectors (`moks/diagnosis/detectors.py`) flag CrashLoopBackOff,
OOMKilled (inside and outside the window), image pull failures, container config errors,
unschedulable/Pending pods, Running-but-not-Ready, evictions, restarts inside the window, Services
whose selector matches no pods (naming the near-miss pods), Services without ready endpoints,
named targetPorts the pods don't declare, under-replicated workloads, Ingress backends that don't
exist and recurring Warning events. DNS issues use `k8s_dns_health`, which reads CoreDNS
pods/endpoints and respects a `kube-system` deny rule.

With `argocd.enabled` in the chart, `k8s_argocd_status` reads Application status from the `argocd`
namespace (`MOKS_ARGOCD_NAMESPACE`). This is how MOKS sees a failed sync, because the workloads stay
healthy. Each app gives its sync status, health, last operation, the error Argo CD reports and its
source paths. Helm parameters and inline values are never copied. If a path is missing and a git
connector is configured, MOKS searches the file names of up to six allowlisted repositories and
lists up to three candidates. The tool is read-only and hides apps whose destination namespace is
outside the user's scope.

## Prometheus

```json
{"base_url": "http://prometheus.monitoring.svc.cluster.local:9090/prometheus",
 "public_url": "https://jithendar.com/prometheus", "timeout_seconds": 15, "auth": "none|bearer|basic"}
```

Instant and range queries go through `POST /api/v1/query[_range]`. The step is computed so that
roughly 120 points cover the window, and queries over 11,000 points are refused. Labels, targets
and alert rules (`/api/v1/rules?type=alert`) are also available. The exact query, window and step
are returned with every result, and `public_url` adds a link that opens the query in Prometheus.

## Loki

```json
{"base_url": "http://loki.monitoring.svc.cluster.local:3100", "timeout_seconds": 20,
 "namespace_label": "namespace", "tenant_id": null, "auth": "none"}
```

`loki_search` builds LogQL from structured fields (namespace, pod, container, case-insensitive
text, trace ID, request ID, errors only), escaping user text so it cannot close a matcher.
`loki_query` accepts LogQL whose every selector pins a permitted namespace.

On mk4, Alloy collects only the namespaces listed in the tools repository's `config.alloy` and
forwards only allowlisted JSON fields. Logs from other namespaces (for example `moks-lab`) are not
in Loki until they are added there. Pod logs through the Kubernetes API still work.

## LLM providers

| Provider | Endpoint | Credential |
|---|---|---|
| `openai` | `https://api.openai.com/v1` or any OpenAI-compatible `/chat/completions` | `env:MOKS_CRED_OPENAI_API_KEY` |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta` | `env:MOKS_CRED_GEMINI_API_KEY` (sent as `x-goog-api-key`) |
| `bedrock` | Converse API in `region`; Messages API on `bedrock-mantle` for `anthropic.claude-*` IDs without a version suffix | none stored: IRSA/Pod Identity, or a mounted profile |
| `ollama` | required, e.g. `http://ollama.ollama:11434` | optional bearer |

Chat and embedding configurations are separate, with one active configuration per purpose.
Changing the active embedding model is refused until Phase 2 reindexing exists. Token use and
estimated cost (from the configured per-million-token prices) are recorded per call, and the daily
budget stops LLM use while investigations continue rule-based. Progress streams as structured
events (tool calls, purposes, results); no private reasoning is requested or shown.

## Grafana

```json
{"base_url": "http://grafana.monitoring.svc.cluster.local/grafana", "public_url": "https://jithendar.com/grafana", "timeout_seconds": 15}
```
plus a credential reference to a service-account token (Viewer). Dashboards link at the
investigation window; panel queries are listed; unified-alerting rules are filtered by namespace scope.

## CloudWatch

```json
{"region": "us-east-1", "allowed_log_groups": ["/aws/eks/prod/*"], "max_window_minutes": 360,
 "max_concurrent_queries": 2, "poll_timeout_seconds": 60, "role_arn": null, "allowed_account_ids": []}
```
Logs Insights queries outside the allowlist are refused, `unmask()` is refused, `| limit` is
appended, and queries that time out or whose investigation is cancelled are stopped
(`StopQuery`). Metrics use `GetMetricData`; alarms use `DescribeAlarms`.

## Git (GitHub)

```json
{"provider": "github", "repositories": ["owner/repo"], "api_url": "https://api.github.com"}
```
An optional token reference is needed for private repositories, and a write-scoped token for the
`open_pull_request` action. Secret-looking paths are never read. `git_compare_deployment` ties a
Deployment to a commit only through a SHA in an image tag or revision annotation; otherwise it
reports that the revision cannot be determined.

## EKS from outside the cluster

```json
{"mode": "eks", "cluster_name": "prod", "region": "us-east-1", "role_arn": "arn:aws:iam::<account>:role/moks"}
```
Endpoint and CA come from `eks:DescribeCluster`, and a bearer token from a presigned STS
`GetCallerIdentity` (same as `aws eks get-token`), refreshed every 10 minutes. Map the role with
an access entry (`deploy/rbac/eks-access-entry.md`).

## FinOps

See [FINOPS.md](FINOPS.md) for `aws_cost` and `cost_model`.

## Tool contracts

All tools are read-only. `*` marks a required argument. Source: `backend/moks/tools/builtin.py`
and `phase2.py`.

| Tool | Connector(s) | Arguments (bounds) | Scope check | Extra permission |
|---|---|---|---|---|
| `k8s_triage` | kubernetes | `namespace`* (≤63) | `namespace` in scope | — |
| `k8s_list_pods` | kubernetes | `namespace`* (≤63), `label_selector`, `unhealthy_only` (default False) | `namespace` in scope | — |
| `k8s_describe_pod` | kubernetes | `namespace`* (≤63), `pod`* (≤253) | `namespace` in scope | — |
| `k8s_pod_logs` | kubernetes | `namespace`* (≤63), `pod`* (≤253), `container`, `previous` (default False), `tail_lines` (≤500, default 150), `since_minutes` | `namespace` in scope | — |
| `k8s_events` | kubernetes | `namespace`* (≤63), `object_name`, `warnings_only` (default True) | `namespace` in scope | — |
| `k8s_workloads` | kubernetes | `namespace`* (≤63) | `namespace` in scope | — |
| `k8s_rollout_history` | kubernetes | `namespace`* (≤63), `deployment`* (≤253) | `namespace` in scope | — |
| `k8s_services` | kubernetes | `namespace`* (≤63) | `namespace` in scope | — |
| `k8s_ingresses` | kubernetes | `namespace`* (≤63) | `namespace` in scope | — |
| `k8s_nodes` | kubernetes | — | environment-wide | — |
| `k8s_dns_health` | kubernetes | — | environment-wide | — |
| `k8s_config_audit` | kubernetes | `namespace`* (≤63) | `namespace` in scope | — |
| `k8s_argocd_status` | kubernetes | `application` (≤253) | environment-wide; apps filtered to scope | — |
| `eks_cluster` | kubernetes | — | environment-wide | — |
| `prom_query` | prometheus | `promql`* (≤2000), `mode` (default range), `minutes` | result filtering / query pinning | metrics.raw_query |
| `prom_pod_resources` | prometheus | `namespace`* (≤63), `pod_regex` (≤200, default .+), `minutes` | `namespace` in scope | — |
| `prom_alerts` | prometheus | `firing_only` (default True) | result filtering / query pinning | — |
| `prom_targets` | prometheus | — | environment-wide | — |
| `prom_metric_names` | prometheus | `search` (≤100) | result filtering / query pinning | — |
| `prom_metric` | prometheus | `metric`*, `namespace` (≤63), `labels` (≤10), `aggregate` (default sum), `by` (≤5), `rate`, `minutes` | `namespace` in scope; without one, metrics.raw_query for all namespaces | — |
| `loki_logs` | loki | `namespace` (≤63; omit for all permitted), `pod`, `container`, `contains`, `trace_id`, `request_id`, `level`, `limit` (≤500, default 200), `minutes` | `namespace` in scope, or filtered to permitted namespaces | — |
| `loki_search` | loki | `namespace`* (≤63), `pod`, `container`, `contains`, `trace_id`, `request_id`, `errors_only` (default False), `limit` (≤500, default 100), `minutes` | `namespace` in scope | — |
| `loki_query` | loki | `logql`* (≤2000), `limit` (≤500, default 100), `minutes` | result filtering / query pinning | — |
| `grafana_dashboards` | grafana | `query`, `tag`, `limit` (≤100, default 20) | result filtering / query pinning | — |
| `grafana_dashboard` | grafana | `uid`* | result filtering / query pinning | — |
| `grafana_alerts` | grafana | `firing_only` (default True) | result filtering / query pinning | — |
| `cw_log_groups` | cloudwatch | `prefix` | environment-wide | — |
| `cw_logs_query` | cloudwatch | `query`* (≤4000), `log_groups`* (≤20), `limit` (≤500, default 100), `minutes` | environment-wide | — |
| `cw_metric` | cloudwatch | `namespace`*, `metric`*, `dimensions`, `stat` (default Average), `minutes` | environment-wide | — |
| `cw_alarms` | cloudwatch | `state` (default ALARM) | environment-wide | — |
| `git_recent_commits` | git | `repository`*, `path`, `hours` (≤720, default 72), `limit` (≤100, default 20) | environment-wide | — |
| `git_read_file` | git | `repository`*, `path`*, `ref` | environment-wide | — |
| `git_compare_deployment` | git, kubernetes | `namespace`* (≤63), `deployment`* (≤253), `repository`* | `namespace` in scope | — |
| `kb_search` | knowledge | `query`* (≤500), `k` (≤12, default 6) | result filtering / query pinning | — |
| `finops_aws_costs` | aws_cost | `group_by` (default service), `tag_key`, `days` (≤90, default 14) | environment-wide | finops.read |
| `finops_aws_forecast` | aws_cost | `days` (≤90, default 30) | environment-wide | finops.read |
| `finops_aws_anomalies` | aws_cost | `days` (≤90, default 30) | environment-wide | finops.read |
| `finops_budgets` | aws_cost | — | environment-wide | finops.read |
| `finops_k8s_allocation` | cost_model, kubernetes, prometheus | `namespace` (≤63; omit for all permitted), `days` (≤30, default 7) | `namespace` in scope | finops.read |
| `finops_rightsizing` | cost_model, kubernetes, prometheus | `namespace` (≤63; omit for all permitted), `days` (≤30, default 7) | `namespace` in scope | finops.read |


Each call produces an evidence row (`E<n>`) with the tool, validated arguments, scope, summary,
redacted data, the exact source (query, API path or link), `collected_at` and duration, plus an
audit record. Interactive reads use the same executor and checks.
