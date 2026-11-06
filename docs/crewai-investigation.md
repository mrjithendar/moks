# CrewAI workload investigation (opt-in)

One use case: *why is this Kubernetes workload unhealthy?* Three CrewAI agents run in sequence and produce
an incident summary in which every finding cites recorded evidence. The LangGraph orchestrator remains the
default for everything else. CrewAI runs only when `MOKS_CREWAI_ENABLED=true` **and** the image was built
with the crewai extra.

Status (2026-11-06): implemented on `feature/crewai-investigation` and left uncommitted. Tested with a
scripted model and fake connectors only. It has not been run against mk4, a real model or a real image.
See [What is still unverified](#what-is-still-unverified).

## 1. How an investigation runs today

| Step | Where | What happens |
|---|---|---|
| UI | `frontend/src/pages/Investigate.tsx` | `POST /investigations` with the question and optional `environment`, `namespace`, `minutes`. Then opens `/conversations/{id}`. |
| API | `api/investigations.py` `start_investigation` | Rate limit and queue limit. `resolve_scope` picks the environment and namespace, `require(P.INVESTIGATION_RUN)` checks the permission and `effective_scope` builds the scope. It stores an `Investigation` row (status `queued`) and a seq-0 `queued` event that carries the W3C `traceparent`, writes an audit record and returns 201. |
| Job | `orchestrator/worker.py` | Workers claim rows with `FOR UPDATE SKIP LOCKED` and hold a lease and token. A watcher cancels the task when `cancel_requested` is set. A run with a checkpoint resumes after a crash. |
| Streaming | `GET /investigations/{id}/events` (SSE) | Streams `investigation_events` rows: `phase`, `tool_call`, `tool_result`, `note`, `llm_usage`, `report`, `done`. |
| Orchestrator | `orchestrator/engine.py` `Investigator._investigate` → `orchestrator/graph.py` | A LangGraph state machine: `intake → knowledge → triage → logs → skills → refresh → plan ⇄ act → correlate → report`, falling back to `rules` without a model. |
| Model | `llm/providers.py`, `llm/budget.py` | MOKS's own HTTP clients for Anthropic, Bedrock, OpenAI, Azure, Gemini and Ollama. Every call goes through `budget.complete`, which handles reservations, daily limits and metering. Tool use is MOKS's own JSON protocol, not vendor function calling. |
| Tools | `tools/builtin.py`, `tools/phase2.py` → `tools/executor.py` | `REGISTRY` holds `ToolSpec`s: argument model, connector, `read_only`, `privileged`, `cluster_scoped`, `extra_permission`. `ToolExecutor.run` is the only path to a live system. It rechecks RBAC (`principal.can`) and scope (`scope.require`), refuses privileged tools, enforces the timeout, redacts and truncates the result, stores it as an `Evidence` row (`E1`, `E2`, …) and audits the call. |
| Identity and scope | `ToolContext` | Built from `load_principal(created_by)`, `effective_scope`, the time window and the environment's connectors. `AccessGuard` rechecks grants while the run is in progress. |
| Tracing | `observability.span` | The `investigation` span continues the queued `traceparent`. Every node and tool call gets a span. Spans record failures by type and code only. |
| Response | `orchestrator/report.py` `Report` | `summary`, `observed_facts[{statement, evidence}]`, `suspected_causes[{cause, confidence, evidence, reasoning}]`, `missing_evidence`, `next_steps`, and more. `validate_claims` and `verify_causes` then check every claim against the evidence ledger in code. |

Feature flags in this repo are `MOKS_*` settings in `config.py`, with booleans such as `rag_rerank_enabled` and `ml_disabled`. Helm sets them from `values.yaml` through `templates/_configmap.yaml`. This flow follows the same pattern: `crewai.enabled` → `MOKS_CREWAI_ENABLED`, default `false`.

## 2. Integration point

`Investigator._investigate` has one branch, `final = await crew_runner.run(...)` instead of
`graph.run(...)`, taken when the seq-0 `queued` event carries a `crewai` block. Only the new route
`POST /investigations/crewai` writes that block. Everything before the branch is unchanged: authorization
at run time, connectors, the `AccessGuard`, the lease and the cancel watcher. Everything after it is the
same code every investigation passes: `validate_claims`, `verify_causes`, relevance, the stored report and
the reply in the conversation.

With the flag off, the route answers 404, `/investigate/config` reports `crewai.available: false` with a
reason, the UI shows no option, and `moks.crew` never imports `crewai`.

## 3. CrewAI version and what was verified

**Pinned: `crewai==1.15.36`** (released 2026-11-06), in `backend/requirements-crewai.txt`. These facts were checked
against that version's docs (`docs/v1.15.36/en/*.mdx` at tag `1.15.36` in
[crewAIInc/crewAI](https://github.com/crewAIInc/crewAI/tree/1.15.36/docs/v1.15.36/en)) and against its installed source.

| Question | Answer for 1.15.36 |
|---|---|
| Python | `>=3.10,<3.14`. The image is Python 3.12. The local test venv for this repo is 3.15, so the crew tests run in a 3.11 venv. |
| Dependency conflict | crewai requires `pydantic<2.13`, and MOKS pins `pydantic==2.14.2`. Nothing else MOKS uses needs pydantic 2.13 or later. The resolver chose **pydantic 2.12.5**. The full backend suite was run on that combination (see the report). Hard dependencies include chromadb 1.5.9, lancedb, onnxruntime, openai, instructor, mcp and pdfplumber, 76 new packages in all. That is why crewai is an image build argument and not part of the default image. |
| How it calls our provider | Neither LiteLLM nor a native SDK. `MoksCrewLLM` subclasses CrewAI's documented custom-LLM extension point `BaseLLM` ([learn/custom-llm.mdx](https://github.com/crewAIInc/crewAI/blob/1.15.36/docs/v1.15.36/en/learn/custom-llm.mdx)). Every call goes to `budget.complete` with the active chat `LLMConfig`, so MOKS's credentials, budget and metering apply. LiteLLM is an optional extra (`crewai[litellm]`) and is not installed. |
| Tool or function calling | MOKS providers expose no vendor function calling, so `supports_function_calling()` returns False. CrewAI then uses its text protocol (`Action:` / `Action Input:` / `Observation:`), which works with any provider that follows instructions. MOKS providers take no stop sequences, so the adapter truncates at `\nObservation:` itself, as the custom-LLM doc describes. Whether a given real model follows the protocol reliably is **unverified**. |
| Sync or async kickoff | `akickoff()` is native async. Observed in 1.15.36: under `akickoff()` the model call (`BaseLLM.call`) and the tool call (`BaseTool._run`) still run **synchronously in a worker thread**, while task callbacks run on the event loop. The adapter hands each call back to the loop (`Bridge`) and refuses, rather than deadlocks, if one ever arrives on the loop thread. |
| Cancellation | CrewAI has no API to interrupt a running step. MOKS closes the Bridge when the run is cancelled, times out or hits a limit. The call in flight is cancelled on the loop, and every later model or tool call raises at once, so the crew stops at its next step. The investigation's own cancel watcher works as before. |
| Telemetry defaults | **On by default.** Anonymous usage spans go to `telemetry.crewai.com:4319` ([telemetry.mdx](https://github.com/crewAIInc/crewAI/blob/1.15.36/docs/v1.15.36/en/telemetry.mdx)). This was confirmed locally: with the variables unset, a run resolved `telemetry.crewai.com` and connected to `167.99.24.219:4319`. MOKS sets `CREWAI_DISABLE_TELEMETRY=true`, `CREWAI_DISABLE_TRACKING=true` and `CREWAI_TRACING_ENABLED=false` before importing crewai, and passes `tracing=False` and `share_crew=False` to the Crew. `OTEL_SDK_DISABLED` is deliberately **not** set, because it would also switch off MOKS's own tracing. A test fails if a run resolves any non-local host. |
| Memory, knowledge, planning | All three call providers of their own. Memory embeds with OpenAI `text-embedding-3-large` and analyses with `gpt-4o-mini` ([memory.mdx](https://github.com/crewAIInc/crewAI/blob/1.15.36/docs/v1.15.36/en/concepts/memory.mdx)). Knowledge embeds with OpenAI `text-embedding-3-small` ([knowledge.mdx](https://github.com/crewAIInc/crewAI/blob/1.15.36/docs/v1.15.36/en/concepts/knowledge.mdx)). Planning uses `gpt-5.4-mini` and needs an OpenAI key ([planning.mdx](https://github.com/crewAIInc/crewAI/blob/1.15.36/docs/v1.15.36/en/concepts/planning.mdx)). **All three are off**, along with delegation, agent reasoning and caching. |
| Other side effects | On import, CrewAI writes `~/.config/crewai` and a credentials directory, and **the import fails when HOME is read-only**, as it is in MOKS pods. `prepare_environment()` moves HOME to `/tmp/moks-crewai` when HOME is not writable. CrewAI also stores every task output in a SQLite file (`latest_kickoff_task_outputs.db`) for `crewai replay`. MOKS swaps that store for a no-op so evidence never lands on the pod's disk. |

## 4. The crew

`Process.sequential` with three agents, each with `max_iter=5`, `allow_delegation=False`, `memory=False` and `max_retry_limit=0`:

1. **Workload investigator** has these tools: `namespace_health` (`k8s_triage`), `workload_status` (`k8s_workloads`),
   `workload_pods` (`k8s_list_pods`), `describe_pod`, `recent_events`, `pod_logs`, `search_logs` (`loki_search`),
   `pod_metrics` (`prom_pod_resources`) and `firing_alerts` (`prom_alerts`).
2. **Change investigator** has `rollout_history`, `config_audit`, `recent_commits` (`git_recent_commits`),
   `deployed_commit` (`git_compare_deployment`) and `search_runbooks` (`kb_search`). When a source is not configured
   or no single repository is set up, the tool returns "not available" and records a gap.
3. **Incident reviewer** has only `evidence_list`, which reads the registry and records nothing. It returns JSON:
   `summary`, `findings[{statement, evidence}]`, `likely_causes[{cause, confidence, evidence, reasoning}]`,
   `missing_evidence` and `next_steps`.

### Evidence registry and validation in code (`moks/crew/evidence.py`)

- Every crew tool call goes through `Investigator._call` and the `ToolExecutor`, so it becomes an `Evidence`
  row. The registry entry is `{id, source, query, timestamp, excerpt, truncated}` (plus `ok`, `kind`, `agent`).
  The excerpt is cut from the stored, redacted result and narrowed to the target workload. The tool
  returns that id to the agent.
- After the crew finishes, `validate()` does the following:
  - It rejects any citation that is not in the registry (`citations_rejected`).
  - It removes any finding or likely cause left with no valid citation (`claims_removed`).
  - It caps confidence: with no live evidence the cause is `low`, with one live source it is at most `medium`,
    and `high` needs live evidence from two sources.
  - It ignores any excerpt or quote the model wrote. `report.crewai.evidence` holds registry excerpts only.
- The engine's usual `validate_claims` and `verify_causes` then run on the result, as they do for every LLM report.

### Response schema

The answer maps onto the existing `Report`: `summary`, `observed_facts`, `suspected_causes` (with
confidence), `missing_evidence` (the crew's gaps are appended in code) and `next_steps`.
**One field was added to the stored report payload: `crewai`**. The `Report` model has no place for the
crew-specific data it holds:

```
crewai: {version, status: completed|partial, stopped, agents[{agent,label,status}], tool_calls, limits,
         refused[{tool,reason}], gaps[{source,reason}],
         validation{citations_rejected, claims_removed, confidence_lowered[, fallback]},
         evidence[registry entries cited, with excerpts], registry[all entries, without excerpts]}
```

There is also one additive field on `GET /investigate/config`: `crewai: {enabled, available, reason, version, llm, limits}`.

## 5. Safety

- **Model provider.** Only the active chat `LLMConfig`, through `budget.complete`. With no active model the
  capability reports unavailable and the route answers 503. If the provider fails during a run, MOKS's
  existing fallback applies: a rule-based report over the evidence collected so far, with the gap listed.
  No other provider, embedding service or telemetry endpoint is contacted (see section 3).
- **Authorization in the tool adapters** (`moks/crew/tools.py`). The model supplies only narrow arguments:
  a pod, a container, a search phrase, a path. The environment, namespace, workload and time window are bound from the
  investigation. Any `namespace` other than the target's, or a pod not named `<workload>-…`, is refused,
  audited (`tool.call`, outcome `not_needed`, reason `crewai: …`) and listed as a gap. Allowed calls still
  pass the `ToolExecutor`'s RBAC and scope checks, so a cluster-scoped Git read by a namespace-scoped person
  is denied and recorded as `access_denied` evidence.
- **Read-only allowlist.** The 14 tools above, all `read_only` and not `privileged`. There is no exec, apply,
  patch, delete, scale or Secret read. A test asserts this against the registry.
- **Untrusted content.** Every tool result reaches the model inside `prompts.untrusted(...)`, a delimiter
  with a random tag. The question is wrapped the same way. The rules in each agent's backstory say this
  content is data. Enforcement does not depend on the prompt, though: the tools, the target and the scope are
  fixed in code, so no log line can widen them. This is tested with a scripted model that obeys an
  injected instruction.

## 6. Limits

| Setting (`MOKS_CREWAI_*`) | Default | Enforced by |
|---|---|---|
| `TIMEOUT_SECONDS` | 120 | `asyncio.timeout` around `akickoff()`. On expiry the report is partial, with the gap listed. `MOKS_MAX_DURATION_SECONDS` still bounds the whole run. |
| `TOOL_TIMEOUT_SECONDS` | 30 | The crew's own `ToolExecutor`. A timeout becomes `tool_error` evidence and a gap. |
| `MAX_ITERATIONS` | 5 per agent | CrewAI `max_iter`, plus a hard cap of max_iter + 2 model calls per agent in the adapter. |
| `MAX_TOOL_CALLS` | 20 | The adapter, which never goes above `MOKS_MAX_TOOL_CALLS` (24 in `values.yaml`). |
| `MAX_CONCURRENT` | 2 per worker process | A process-wide counter. The run waits up to `SLOT_WAIT_SECONDS` (20), then fails with a clear error. |
| `EXCERPT_CHARS` / `LOG_LINES` | 2000 / 100 | Excerpt length and log tail. The executor's `max_result_bytes` (48 KB) still bounds every stored result. |

Progress is emitted per agent as `phase` `crewai_workload`, `crewai_change` and `crewai_review`, plus a "finished" note.
The investigation span is the parent of a `crewai investigation` span, which is the parent of one `crewai task <agent>`
span per agent. Each task span contains `crewai llm` and `crewai tool …` spans, the latter wrapping the executor's
`tool …` span, so one trace ID covers the whole run. Span attributes carry names, refs and token counts only.
CrewAI's own `verbose` output is off.

Known limitation: a CrewAI run writes no checkpoints, so a run whose worker dies is marked failed by the
reaper instead of resuming.

## 7. Enable, verify, roll back

CI builds and Argo CD deploys (`.github/workflows/ci.yml`):

- **release** builds the arm64 image on every push to master. Repository variable `MOKS_CREWAI=true` builds it with
  the crewai extra, and CI then checks that crewai imports at the pinned version on a read-only root with telemetry off.
- **deploy** runs when variable `MK4_DEPLOY=true`. It asks Argo CD to sync the MOKS Applications from master in the
  documented order, then runs Argo CD's restart action on `moks-worker`, `moks-api`, `moks-executor` and `moks-terminal`,
  so they pull the new `:latest`. Finally it checks that `/moks/api/health` reports the commit. CI holds no Kubernetes credentials, only a
  token for Argo CD project role `moks/ci` (get, sync, restart).
- **crewai** runs on pull requests: the backend suite on the CrewAI dependency set.

One-time setup, then every push to master deploys:

```sh
scripts/setup-argocd-ci.sh                          # applies the AppProject with role ci, stores ARGOCD_AUTH_TOKEN, sets MK4_DEPLOY
gh variable set MOKS_CREWAI --repo mrjithendar/moks --body true
# deploy/helm/moks/values-minikube.yaml: crewai.enabled: true, then commit and push to master
```

Verify:

```sh
curl -s -b cookies https://jithendar.com/moks/api/investigate/config | jq .crewai     # available: true, version 1.15.36
scripts/crewai-demo.sh up                                                             # namespace crewai-demo, two broken workloads
# Investigate -> "CrewAI investigation" -> namespace crewai-demo, workload web (then worker)
scripts/crewai-demo.sh down
```

Expected results: for `web`, an ImagePullBackOff on `busybox:1.38-does-not-exist`, cited from
`namespace_health`, `describe_pod` and events. For `worker`, a CrashLoopBackOff with `QUEUE_URL is not set`
from the previous container's logs. The injected log line must change nothing.

Roll back: set `crewai.enabled: false` and push (the route answers 404 again and the option disappears), or set
`MOKS_CREWAI=false` so the next build is the default image (the capability then reports "not installed"). There is no migration.
To stop CI deploying, set `MK4_DEPLOY=false`.

## What is still unverified

- No real model has run the crew. Small models may not follow the Action/Observation format. A model that
  writes no valid JSON at the end gets the rule-based report, which is correct but less useful.
- The image with `MOKS_CREWAI=true` has never been built; CI's release job is the first place it will be. Its import check
  fails the build if a linux/arm64 wheel is missing (onnxruntime, google-re2, lancedb and pyarrow are the likely ones).
- No mk4 smoke test has been run: the demo namespace needs a cluster write, and the run needs the image.
