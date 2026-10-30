# Testing

## Results (2026-10-30, Phases 1–4, FinOps, skills)

| Suite | Result | Environment |
|---|---|---|
| Backend unit + integration (`backend/tests`) | **109 passed**, 4 live tests skipped | Python 3.11, PostgreSQL 17 (fresh database per run) |
| Lab evaluation (`moks eval lab`, rules mode) | **score 1.000** on 6 cases (selector 502, crashloop, OOM, latency after rollout, prompt injection, no false positives) | local + fixture connectors |
| Live read-only checks (`MOKS_LIVE=1 tests/live`) | **4 passed** | mk4: Kubernetes v1.37.0, Prometheus 3.15.0, Loki (via `kubectl port-forward`) |
| Frontend (`npm test`, `tsc --noEmit`, `vite build`) | **5 passed**, typecheck clean, build 99 KB gzip JS | Node 26 |
| Helm (`helm lint` for minikube and EKS values) | clean | Helm 4.3 |
| Rendered chart, `kubectl apply --dry-run=server` on mk4 | all 34 objects accepted (diagnostics objects checked in an existing namespace) | mk4 API server (nothing created) |
| Terraform `validate` + read-only `plan` | valid; +1 resource (FinOps read policy) | account 881026659929 |
| End-to-end UI walkthrough in headless Chrome | passed, no unexpected console errors | Local API + worker + built SPA at `/moks`, live connectors to mk4 |

### Live end-to-end walkthrough (headless Chrome, live cluster data)

These steps were run against the real cluster:

1. Opened a deep link while signed out, was redirected to `/moks/login?next=…`, signed in and
   returned to the deep link.
2. **Overview → Check live health**: 4/4 nodes ready, pods needing attention in the last hour,
   firing alerts.
3. Started "Show pods restarting in the last 30 minutes." in `mk4/roboshop-ai`. Progress
   streamed over SSE (scope, baseline `k8s_triage`, `prom_alerts`, `loki_search`), and the
   rule-based report cited `E1`–`E3`.
4. Refreshed the investigation page (deep link). Explorer → pods → pod drawer → logs. A PromQL
   memory chart with the exact query and a Prometheus link. Loki search. Audit → Verify chain
   (intact).

These runs found and fixed several real defects:

- Old restarts (from node reboots) counted as current problems; restart and OOM detection are now window-aware.
- An all-namespace investigation was readable by namespace-scoped viewers; `*` now needs a namespace-`*` binding.
- Literal values of sensitive env vars appeared in pod details; they are now masked by name.
- An environment deny list hid all non-namespaced data from administrators; "environment-wide" and "unrestricted" scope are now separate.

### Phases 2–4 live checks (read-only, against mk4)

- Connector tests: Kubernetes, Prometheus, Loki and cost model OK; Grafana reachable (13.2.4) but **401 without a token**.
- FinOps on `roboshop-ai`: allocation for 16 workloads; rightsizing flagged **MongoDB within 10% of its
  memory limit** and RabbitMQ CPU above request. An investigation ("which pods are close to their
  memory limits?") selected the `oomkilled-memory` skill and cited that as a fact with a
  `patch_resources` suggestion.
- GitHub ingestion of `mrjithendar/moks` (`docs/**`, `lab/README.md`) at commit `03732fc`: 7
  documents. Search returned the rollout section with a line-anchored GitHub URL.
- An alert webhook opened an incident and an automatic read-only investigation; the repeat alert was deduplicated.
- An action plan for `Deployment/mongodb` (`patch_resources`) was created from a rightsizing row
  with the live `resourceVersion`. It was **not executed**: no executor ran locally.

Defects found and fixed during this round:
- A race where the DB commit happened after the response was sent (sign in → 401; seen 1 in 3
  locally). This also affects the deployed Phase 1, and the next rollout fixes it. A guard test
  enforces `scope="function"`.
- The Prometheus poller resolved webhook-opened incidents.
- The rate-limit decision was misreported.
- Evaluation environments leaked into environment pickers.
- Policy denials returned 409 instead of 403.

### Portal UX checks (headless Chrome, real API, simulated connectors)

These ran the real backend and built frontend against PostgreSQL, with simulated Kubernetes,
Prometheus and Loki connectors and two environments, one of them with a failing namespace. **They
are not checks against mk4.** Results at that point: 120 backend tests passed (4 skipped), 5 frontend tests passed,
Helm lint clean.

Driven in the browser: Overview polling every 5 s, all Explorer tabs and pod drawer, Metrics catalog
and label filters, Logs filters and line details, Audit filters, FinOps by environment, creating an
EKS environment with its data sources, editing a connector, and an investigation with no
environment or namespace.

Bugs this found and fixed: connector errors during fan-out collided on one database session; one
unreadable namespace failed the whole FinOps report; the name field's validation pattern was invalid
in current Chrome and was silently ignored.

### Enterprise upgrade (simulated connectors, not mk4)

Backend suite 232 passed, 4 skipped. Frontend 10 passed. Helm lint and render clean for Minikube and EKS.
A database with existing rows was upgraded from `0003` to `0008`: approved plans expired, a mid-flight plan
went to reconciliation, the in-cluster connector gained an explicit write target, the EKS connector stayed
read-only, and an explicit 360-minute schedule was kept. Chrome drove Metrics, saved queries, Detection
dry run, Skills, Knowledge, Investigate and deep links with no console errors.

## Coverage of the required areas

| Area | Tests |
|---|---|
| RBAC and scope isolation | `test_rbac_scope.py` (roles, globs, global-only admin, raw-PromQL rule, intersection with environment allow/deny); `test_investigations.py::test_namespace_scoped_user_cannot_investigate_other_namespaces`, `test_environment_denylist_overrides_rbac`, `test_investigations_are_invisible_outside_scope`, `test_revoked_access_is_rechecked_at_execution` |
| Prompt injection in logs/documents | `test_prompt_injection_cannot_widen_tools_or_scope`: malicious log lines plus a scripted LLM that requests `scale_deployment`, another namespace, a cluster-scoped tool and out-of-bounds args. All are denied or rejected and audited, secrets are absent from prompts, and fabricated citations are dropped |
| Query bounds and cancellation | `test_window_is_clamped_to_environment_maximum`, `test_auto_step_bounds_points`, `test_prometheus_rejects_unbounded_ranges`, `test_truncate_json_bounds_size`, LogQL pinning tests, `test_cancellation_stops_a_running_investigation`, `test_time_limit_fails_cleanly` |
| Connector failure handling | `test_prometheus_errors_are_classified_and_retried` (503 retried, 401, 404 hint, parse error, timeout, oversize), `test_unreachable_connector_message`, `test_unavailable_connector_is_reported_not_hidden`, `test_credential_references` |
| `/moks` routing, auth, streaming | `test_routing_auth.py` (308 to `/moks/`, deep links, assets vs SPA fallback, traversal, JSON 404 under `/moks/api`, cookie `Path=/moks`, CSRF + Origin, logout, dev mode refused in production, open-redirect guard, local-login rate limit); `test_sse_stream_replays_events_and_ends` (content type, ordering, `Last-Event-ID` resume); frontend `api.test.ts` (base path, CSRF header, 401 → `/moks/login?next=`) |
| Audit | append-only triggers, chain verification, tamper detection after a superuser bypass, redacted details |
| Retrieval access controls | `test_rag.py::test_retrieval_respects_access_scope` (namespace-scoped, glob and no-binding users), deletion propagation, secret exclusion/redaction |
| Prompt injection in documents | `test_rag.py::test_prompt_injection_in_documents_cannot_trigger_actions` (a retrieved runbook demanding scale-to-zero; the request is denied and audited) |
| Approval expiry, replay, parameter changes | `test_actions.py` (self-approval refused, hash mismatch, expiry, replay, parameters tampered after approval, revoked approver, resource changed after review, policy limits, forbidden patch fields) |
| Remediation validation and failure recovery | `test_actions.py::test_validation_failure_triggers_automatic_recovery`, `test_full_flow_with_separation_of_duties` |
| Diagnostics | `test_diagnostics_evals.py` (catalog-only commands, SSRF guard, approval for elevated/production, TTL cleanup) |
| Alerts | `test_phase4_alerts.py` (webhook auth, dedup, cooldown, hourly cap, opt-in, deny lists, poller scope, incident report, postmortem into knowledge) |
| Phase 2 connectors and FinOps | `test_phase2_connectors.py` (Grafana, Git never guesses, EKS token, Cost Explorer cache/cap, allocation and rightsizing math, finops permission) |
| SRE skills | `test_skills.py` (load, validation, custom override, selection, executor-checked steps, allowlisted suggestions) |

The fake Kubernetes connector (`tests/fakes.py`) exercises MOKS's own logic. It is **not**
evidence of a live integration; the `tests/live` suite and the walkthrough above provide that.

## Running

```sh
cd backend
MOKS_TEST_DATABASE_URL=postgresql://<user>@localhost:5432/postgres python -m pytest -q   # needs CREATE DATABASE rights

# live, read-only (see the module docstring)
MOKS_LIVE=1 MOKS_CREDENTIALS_DIR=~/.moks-dev MOKS_LIVE_CONTEXT=mk4 \
MOKS_LIVE_PROMETHEUS=http://localhost:19090/prometheus MOKS_LIVE_LOKI=http://localhost:13100 \
python -m pytest -q -s tests/live

cd ../frontend && npm test && npm run build
helm lint deploy/helm/moks -f deploy/helm/moks/values-minikube.yaml
```

## Not yet verified (Phases 2–4)

- Executor writes, diagnostic pods and OpenSearch have **not run on mk4**. They are covered by fakes
  and server-side dry runs; deploy, then exercise them on `moks-lab` (lab/README.md).
- CloudWatch, Cost Explorer/Budgets, remote EKS and OpenSearch were tested against stubs or mock
  transports only (no credentials or cluster available). Grafana needs a token.
- No LLM provider was called; LLM mode is tested with a scripted OpenAI-compatible endpoint.

## Not yet verified (Phase 1, original list)

- The container image has not been built: there was no Docker daemon on the development machine.
  CI's `release` job builds it on `ubuntu-24.04-arm` and smoke-tests `/healthz`, `/moks/` and
  `/moks/api/health` before pushing.
- Nothing has been deployed to mk4. The Ingress, NetworkPolicies and Traefik behaviour for SSE
  were validated only by server-side dry run and design (no compress middleware). Verify with
  step 7 of the operator guide.
- No LLM provider was called. The LLM path is covered by a scripted OpenAI-compatible endpoint.
  Gemini, Bedrock and Ollama adapters follow the documented request formats but are untested
  against real endpoints, because no credentials were available. Configure one and use
  **LLM providers → Test**.
- OIDC was tested only for its guards (state/nonce/PKCE code paths, open-redirect protection),
  not against a real IdP.
- The demo lab has not been applied to mk4.
