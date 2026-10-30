# Testing

## Results (2026-10-30, Phase 1)

| Suite | Result | Environment |
|---|---|---|
| Backend unit + integration (`backend/tests`) | **67 passed**, 4 live tests skipped | Python 3.11, PostgreSQL 17 (fresh database per run) |
| Live read-only checks (`MOKS_LIVE=1 tests/live`) | **4 passed** | mk4: Kubernetes v1.37.0, Prometheus 3.15.0, Loki (via `kubectl port-forward`) |
| Frontend (`npm test`, `tsc --noEmit`, `vite build`) | **5 passed**, typecheck clean, build 99 KB gzip JS | Node 26 |
| Helm (`helm lint` for minikube and EKS values) | clean | Helm 4.3 |
| Rendered chart, `kubectl apply --dry-run=server` on mk4 | all 21 resources accepted | mk4 API server (nothing created) |
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

## Coverage of the required areas

| Area | Tests |
|---|---|
| RBAC and scope isolation | `test_rbac_scope.py` (roles, globs, global-only admin, raw-PromQL rule, intersection with environment allow/deny); `test_investigations.py::test_namespace_scoped_user_cannot_investigate_other_namespaces`, `test_environment_denylist_overrides_rbac`, `test_investigations_are_invisible_outside_scope`, `test_revoked_access_is_rechecked_at_execution` |
| Prompt injection in logs/documents | `test_prompt_injection_cannot_widen_tools_or_scope`: malicious log lines plus a scripted LLM that requests `scale_deployment`, another namespace, a cluster-scoped tool and out-of-bounds args. All are denied or rejected and audited, secrets are absent from prompts, and fabricated citations are dropped |
| Query bounds and cancellation | `test_window_is_clamped_to_environment_maximum`, `test_auto_step_bounds_points`, `test_prometheus_rejects_unbounded_ranges`, `test_truncate_json_bounds_size`, LogQL pinning tests, `test_cancellation_stops_a_running_investigation`, `test_time_limit_fails_cleanly` |
| Connector failure handling | `test_prometheus_errors_are_classified_and_retried` (503 retried, 401, 404 hint, parse error, timeout, oversize), `test_unreachable_connector_message`, `test_unavailable_connector_is_reported_not_hidden`, `test_credential_references` |
| `/moks` routing, auth, streaming | `test_routing_auth.py` (308 to `/moks/`, deep links, assets vs SPA fallback, traversal, JSON 404 under `/moks/api`, cookie `Path=/moks`, CSRF + Origin, logout, dev mode refused in production, open-redirect guard, local-login rate limit); `test_sse_stream_replays_events_and_ends` (content type, ordering, `Last-Event-ID` resume); frontend `api.test.ts` (base path, CSRF header, 401 → `/moks/login?next=`) |
| Audit | append-only triggers, chain verification, tamper detection after a superuser bypass, redacted details |
| Retrieval access controls | Phase 2 (no RAG yet) |
| Approval expiry/replay/parameter changes; remediation validation | Phase 3 (no remediation yet) |

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

## Not yet verified

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
