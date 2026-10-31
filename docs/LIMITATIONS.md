# Status and remaining limitations

## Phase status

| Phase | Scope | State |
|---|---|---|
| 1 | Login + RBAC, `/moks` portal, Kubernetes/Prometheus/Loki, cited investigations, streaming, audit, Helm/Argo | Verified live, except streaming through the ingress, which has not been checked |
| 2 | EKS (remote), CloudWatch, Grafana, Git connectors; RAG (Postgres/pgvector/OpenSearch); incident workspaces | Git and keyword RAG verified live. Vector search, Grafana, CloudWatch and EKS are not: see below |
| 3 | Approvals, allowlisted remediation with pre/post validation and recovery, diagnostic sessions | Tested with fakes only. No plan has run against a real API server |
| 4 | Alert-triggered investigations (webhook + poller) with dedup/cooldowns, incident reports and postmortems, evaluation datasets | Tested with fakes. Not exercised on mk4 since the rewrite |
| + | FinOps (AWS cost, anomalies, budgets, Kubernetes allocation and rightsizing, LLM spend) | Kubernetes allocation runs live but reports 0 until a price is set. AWS side tested with fakes only |
| + | SRE skills (`SKILL.md`) | 50 built-in skills; custom skills via ConfigMap |

See [TESTING.md](TESTING.md#live-status-2026-10-30) for the evidence behind each line.

## Not built from the enterprise brief

- **Metrics filters:** Explore filters by environment, data source, namespace, resource type and the
  metric's real labels. There are no account, region or cluster filters; CloudWatch has its own tab with
  basic discovery. Scoped discovery checks up to 20 namespaces per source. Query budgets are constants in code, with no admin screen.
- **Investigations:** no trace backend integration. Resumption means re-running or a follow-up, not
  continuing a stopped run. Grouped runs produce one investigation per environment, not one correlated report.
- **Detection:** no Grafana alert source; Prometheus alerts, Kubernetes state, Loki and CloudWatch alarms only.
- **Skills:** `evaluation` entries describe intended coverage. The cases are tests in `backend/tests`; the
  evaluation runner does not read them. Some skills reference metrics that vary by deployment.
- **Argo CD:** read-only, through the Kubernetes connector. MOKS reads Application status but cannot sync, refresh or see diffs, and suggests where a missing path moved without knowing which candidate is right. Apps outside the reader's namespaces are hidden.

## Conversations and logs

- **Delete is soft.** A deleted message or conversation is hidden from people and from MOKS and can be
  restored, but stays in PostgreSQL and in backups. Evidence from a turn stays too. Text a person pasted by
  mistake, such as a secret, is therefore not erased. Removing it needs a database action by an administrator.
- **New messages appear within about 4 seconds:** the page polls. There are no notifications when someone
  adds you or replies.
- **Logs:** up to 3 pods are read per turn, with the last 150 lines of each container. A cause outside that
  tail is missed. Previous logs exist only for the last restart; older ones are in Loki at best, and on mk4
  Loki keeps only allowlisted JSON fields from listed namespaces.
- **Verified from logs** means the process itself reported this failure just before exiting. It is the
  stated reason, and the underlying cause can lie deeper.

## Pinecone

Tested against a stand-in for the `2026-07` API only: no real Pinecone call has been made. Each reindex needs
a new index, and a reindex holds both until it finishes, so a plan limit on indexes can block it. Changing
backends does not move vectors: run a reindex afterwards, which pays to embed everything again.

## Durability

- **Single disk:** all four mk4 nodes are containers on one disk, and the PostgreSQL and OpenSearch volumes
  have reclaim policy `Delete`. Until backups are running, a disk failure or `minikube delete` loses everything.
- **No point-in-time recovery:** backups are logical dumps, so at most about an hour is lost once hourly
  backups run. WAL archiving would narrow that to minutes, but needs a PostgreSQL restart, a custom image or
  a shipping sidecar, and base backups.
- **Vectors:** with the OpenSearch backend, embeddings exist only in OpenSearch. Chunk text is in PostgreSQL,
  so the index can be rebuilt, but that means paying to embed everything again.
- **Deletes are permanent:** deleting a knowledge source, connector or saved query removes its rows.

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
- **Executor:** the write path has only been tested with an in-memory writer. No plan has run against a
  real API server.
- **FinOps:** allocation covers current pods only; Cost Explorer lags up to 24 h and costs $0.01/request (cached and capped).
- **Multi-environment views:** each environment queries at most 40 namespaces (the view says so
  when it stops), 6 at a time. Log results are merged and cut to the line limit, so a busy
  environment can crowd out a quiet one; filter by environment to see it. The metric catalog shows
  3,000 names per environment.
- **Analyze with AI** covers one environment: the one selected, or the default when the filter is
  "All environments".
- **EKS:** MOKS reads workloads, the control plane (`eks_cluster`: version, node groups, add-ons,
  Fargate profiles) and CloudWatch. It changes only Deployments, Services and HPAs through approved
  plans, and opens pull requests. Node groups, add-ons, ConfigMaps and Secrets are not changed by
  MOKS; configuration fixes are proposed as pull requests or suggested steps.
- **Evaluation:** fixture-based datasets only test detectors and report discipline against
  recorded state; they do not measure LLM reasoning quality beyond the listed checks.

## Known limitations (Phase 1)

**Integrations**
- Gemini embeddings have been called live from a pod (`gemini-embedding-001`, 3072 dimensions). No
  chat model has answered a live request: Bedrock has no AWS keys on mk4. OpenAI and Ollama were only
  tested with a scripted server.
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
