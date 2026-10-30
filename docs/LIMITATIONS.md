# Status and remaining limitations

## Phase status

| Phase | Scope | State |
|---|---|---|
| 1 | Login + RBAC, `/moks` portal, migrations, Kubernetes/Prometheus/Loki connectors, LLM-assisted investigation with cited live evidence, streaming, audit, Helm/Argo deployment | **Implemented and tested.** Not yet deployed (see the operator guide) |
| 2 | EKS (remote), CloudWatch, Grafana, Git connectors; OpenSearch RAG; incident workspaces | Designed ([ARCHITECTURE.md](ARCHITECTURE.md)); chart switch for the vector backend only |
| 3 | Approvals, allowlisted remediation, diagnostic sessions, pre/post verification | Designed; no code. All remediation is a non-executable proposal |
| 4 | Alert-triggered investigations, incident reports, evaluation datasets | Not started |

## Known limitations (Phase 1)

**Integrations**
- Grafana, CloudWatch, Git and remote EKS are not implemented. Their connector rows can be saved,
  but they report "planned".
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
