# Connectors and tool contracts

Connectors are configured per environment in **Administration → Connectors** (or seeded from
Helm values). A row holds non-secret JSON config and a credential *reference*. **Test** runs a
real request and stores the outcome, which the Overview shows. Only the first enabled connector
of each type per environment is used.

## Status

| Connector | Phase | State |
|---|---|---|
| Kubernetes (Minikube; EKS from inside the cluster) | 1 | Implemented. Verified live against mk4 (Kubernetes v1.37). |
| Prometheus | 1 | Implemented. Verified live against mk4 (Prometheus 3.15, `/prometheus` prefix). |
| Loki | 1 | Implemented. Verified live against mk4. |
| Grafana, CloudWatch, Git (GitHub) | 2 | Not implemented. Rows can be saved; **Test** answers "planned for Phase 2". |
| EKS from outside the cluster | 2 | Not implemented: the image has no `aws` CLI for exec-based kubeconfigs. Native STS token generation is planned. |

## Common behaviour

- **Timeouts:** per connector (`timeout_seconds`), with a hard per-tool-call ceiling of 45 s
  inside investigations and 30 s for interactive reads.
- **Retries:** idempotent GETs retry twice on timeouts, connection errors, 429 and 5xx, with
  backoff of 0.3 s and 0.9 s. 4xx responses are not retried.
- **Size:** responses over 8 MB are refused (`too_large`). Results are redacted, then truncated
  structurally to `MOKS_MAX_RESULT_BYTES` (48 KB) with `truncated: true`.
- **Pagination:** Kubernetes lists page 250 items at a time up to 1,000 per call. Prometheus
  results cap at 25 series × 240 points (downsampled). Loki returns at most 500 lines of
  2,000 characters each.
- **Errors** carry a kind and a message the user can act on: `config` (bad setup or missing
  credential), `auth`, `forbidden` (credential lacks RBAC), `unavailable`, `timeout`,
  `bad_request` (with the backend's own error, such as a PromQL parse error), `not_found` (with a
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
| `bedrock` | Converse API in `region` | none stored: IRSA/Pod Identity, or a mounted profile |
| `ollama` | required, e.g. `http://ollama.ollama:11434` | optional bearer |

Chat and embedding configurations are separate, with one active configuration per purpose.
Changing the active embedding model is refused until Phase 2 reindexing exists. Token use and
estimated cost (from the configured per-million-token prices) are recorded per call, and the daily
budget stops LLM use while investigations continue rule-based. Progress streams as structured
events (tool calls, purposes, results); no private reasoning is requested or shown.

## Tool contracts

All tools are `read_only=True`. The executor also enforces the investigation window. `*` marks a
required argument. Source: `backend/moks/tools/builtin.py`.

| Tool | Connector | Arguments (bounds) | Scope check | Extra permission |
|---|---|---|---|---|
| `k8s_triage` | kubernetes | `namespace`* (≤63) | `namespace` in scope | — |
| `k8s_list_pods` | kubernetes | `namespace`* (≤63), `label_selector`, `unhealthy_only` (default False) | `namespace` in scope | — |
| `k8s_describe_pod` | kubernetes | `namespace`* (≤63), `pod`* (≤253) | `namespace` in scope | — |
| `k8s_pod_logs` | kubernetes | `namespace`* (≤63), `pod`* (≤253), `container`, `previous` (default False), `tail_lines` (≤500, ≥1, default 150), `since_minutes` | `namespace` in scope | — |
| `k8s_events` | kubernetes | `namespace`* (≤63), `object_name`, `warnings_only` (default True) | `namespace` in scope | — |
| `k8s_workloads` | kubernetes | `namespace`* (≤63) | `namespace` in scope | — |
| `k8s_rollout_history` | kubernetes | `namespace`* (≤63), `deployment`* (≤253) | `namespace` in scope | — |
| `k8s_services` | kubernetes | `namespace`* (≤63) | `namespace` in scope | — |
| `k8s_ingresses` | kubernetes | `namespace`* (≤63) | `namespace` in scope | — |
| `k8s_nodes` | kubernetes | — | cluster-wide | — |
| `k8s_dns_health` | kubernetes | — | cluster-wide | — |
| `prom_query` | prometheus | `promql`* (≤2000), `mode` (default range), `minutes` | query content / result filtering | metrics.raw_query |
| `prom_pod_resources` | prometheus | `namespace`* (≤63), `pod_regex` (≤200, default .+), `minutes` | `namespace` in scope | — |
| `prom_alerts` | prometheus | `firing_only` (default True) | query content / result filtering | — |
| `prom_targets` | prometheus | — | cluster-wide | — |
| `loki_search` | loki | `namespace`* (≤63), `pod`, `container`, `contains`, `trace_id`, `request_id`, `errors_only` (default False), `limit` (≤500, ≥1, default 100), `minutes` | `namespace` in scope | — |
| `loki_query` | loki | `logql`* (≤2000), `limit` (≤500, ≥1, default 100), `minutes` | query content / result filtering | — |

Each call produces an evidence row (`E<n>`) with the tool, validated arguments, scope
(environment, namespace, window), summary, redacted data, the exact source (query or API path),
`collected_at` and duration, plus an audit record. Interactive explorer reads use the same
executor and checks, but store only the audit record.
