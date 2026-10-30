# Status and remaining limitations

## Phase status

| Phase | Scope | State |
|---|---|---|
| 1 | Login + RBAC, `/moks` portal, Kubernetes/Prometheus/Loki, cited investigations, streaming, audit, Helm/Argo | Deployed on mk4 |
| 2 | EKS (remote), CloudWatch, Grafana, Git connectors; RAG (Postgres/pgvector/OpenSearch); incident workspaces | Implemented and tested; not yet deployed |
| 3 | Approvals, allowlisted remediation with pre/post validation and recovery, diagnostic sessions | Implemented and tested with fakes; executor never run against a cluster |
| 4 | Alert-triggered investigations (webhook + poller) with dedup/cooldowns, incident reports and postmortems, evaluation datasets | Implemented and tested |
| + | FinOps (AWS cost, anomalies, budgets, Kubernetes allocation and rightsizing, LLM spend) | Implemented; Kubernetes side verified live, AWS side stub-tested |
| + | SRE skills (`SKILL.md`) | 13 built-in skills; custom skills via ConfigMap |

## Known limitations (Phases 2–4)

- **Remediation** covers Deployments, Services and HPAs; StatefulSet/DaemonSet actions are not allowlisted.
  Validation checks rollout status and pod health, not application SLOs (add a Prometheus query
  validation later). Recovery restores the pod template and replicas, not external side effects.
- **Diagnostic** commands are a fixed catalog; there is no interactive terminal (deliberately).
  Elevated sessions install OS packages as root inside the disposable pod.
- **RAG:** UTF-8 text formats only (no PDF/Office parsing). Reranking is RRF fusion, not a
  cross-encoder. pgvector search is exact (no ANN index). GitHub sync reads at most 3,000 files of
  300 KB per source.
- **Alerts:** the poller reads Prometheus rule state only (no Alertmanager silences or inhibition).
  The automatic-investigation cap (6/hour/environment) is fixed in code.
- **FinOps:** allocation covers current pods only; Cost Explorer lags up to 24 h and costs $0.01/request (cached and capped).
- **Evaluation:** fixture-based datasets only test detectors and report discipline against
  recorded state; they do not measure LLM reasoning quality beyond the listed checks.

## Known limitations (Phase 1)

**Integrations**
- No LLM provider has been exercised against a real endpoint; only the OpenAI-compatible
  protocol was tested, with a scripted server. Gemini/Bedrock/Ollama request formats follow
  their public APIs as of this writing.
- Token counts come from provider responses. Cost is an estimate from prices an admin
  configures; nothing is fetched from a pricing API.
- Responses stream as structured progress events. The final report arrives whole, not
  token by token.

**Diagnosis**
- Rule-based detectors cover Kubernetes-level failures. Application-level causes (for example
  latency without a rollout) need an LLM or a human reading the cited logs and metrics.
- Rollout correlation is timing only and is labelled as such. Image digests are not compared
  with Git (Phase 2).
- Raw PromQL cannot be confined to a namespace, so it is limited to namespace-`*` bindings.
  Namespace-scoped users get templated cAdvisor queries only. mk4 has no kube-state-metrics, so
  restart counts come from the Kubernetes API, not metrics.
- Loki on mk4 keeps only allowlisted JSON fields from listed namespaces, so free-text error
  searches find little there. Pod logs from the Kubernetes API are complete.

**Security**
- Rate limits are per process.
- Redaction is pattern- and name-based and cannot recognise every secret shape.
- Audit integrity is tamper-evident, not tamper-proof, against a database superuser.
  [SECURITY.md](SECURITY.md#audit) describes external archival.
- The Kubernetes connector shares one ServiceAccount across users. Per-user infrastructure
  identity (impersonation) is not implemented, so isolation between users relies on MOKS's
  RBAC + scope checks, which are covered by tests.
- With `authMode: local` there is no MFA. Use OIDC with an MFA-enforcing IdP for production use.

**Operations**
- The worker runs investigations in-process with asyncio. For many concurrent investigations,
  scale `worker.replicas` (claiming is `SKIP LOCKED`-safe). There is no Redis.
- The SSE endpoint polls PostgreSQL every 0.5 s per open stream, which is fine for tens of
  viewers. LISTEN/NOTIFY is a later optimisation.
- Investigations, evidence and audit rows have no retention policy yet.
- The image has not been built or deployed from this workstation (no Docker daemon). CI builds it.
