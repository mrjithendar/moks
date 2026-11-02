# Reranking

An optional internal service that reorders passages search has already authorized, using a small pretrained
cross-encoder on CPU. It is **off by default**. Nothing is trained, and no GPU, third-party API or change to the LLM is involved.

```text
keyword and vector retrieval (filtered by access)
  → authoritative access and lifecycle checks in PostgreSQL
  → bounded candidates: no exact repeats, a limit per document
  → reranking service: returns ids and scores only
  → exact identifiers kept first, superseded documents kept last
  → diversity selection, recheck against PostgreSQL, citations
  → the existing LLM report
```

A score orders one result list. It is not a probability, and it does not show that a passage supports an answer. It feeds nothing except position: not `evidence.state`, abstention, claim validation, verification, skill selection, severity or any approval. Text and citations never leave MOKS's records, and the service cannot rewrite them.

If anything goes wrong, search returns its existing order: timeout, overload, an unreachable or unready service, a changed model revision, or an answer that is not exactly one finite score for each id sent. After 5 failures in a row the client stops calling for 30 seconds, or for the service's `Retry-After` when it is overloaded. Search reports this as `reranking.state: fallback` with a reason.

## Model

`cross-encoder/ms-marco-MiniLM-L6-v2` at commit `233902d25c440f23af6f7d6e94d2946bac0bee0a`.

| | |
|---|---|
| License | Apache-2.0 (model card) |
| Language | English only. Other languages will rank poorly |
| Trained on | MS MARCO web passage ranking, not operations documents |
| Size | 6 layers, `model.safetensors` 90.9 MB; 88 MB on disk for the files used |
| Input | 512 tokens for the query and passage together, set explicitly; longer pairs are truncated and reported |
| Output | Raw logits. `activation_fn` is set to identity, because the library's default sigmoid makes them look like probabilities |
| Safety | `safetensors` only, `trust_remote_code=False`, `local_files_only=True` |

Published figures are NDCG@10 74.30 on TREC DL 2019 and MRR@10 39.01 on MS MARCO dev. Its throughput figure of 1800 documents per second is from a V100 GPU and does not apply here.

## Delivery

`reranker/Dockerfile` installs pinned dependencies with CPU-only torch, downloads the model at the pinned revision, and verifies every file against `reranker/model.lock.json`, which holds SHA-256 for each. The service loads from local files with Hugging Face offline mode on, so a running pod needs no network, and its NetworkPolicy allows none.

CI builds on every push to `master` and pushes the commit tag and `latest`. The chart runs `reranker.image.tag` (`latest`) with `imagePullPolicy: Always`, so a pod gets the newest build when it restarts. `reranker.image.digest` replaces the tag if set. As with the main image, `latest` means two pods can run different builds until both restart.

**To change the model:** update `model.lock.json` with the new revision and hashes, rerun the evaluation, push, then set `reranker.model.revision` and restart the reranker, API and worker. Until the revisions agree, MOKS refuses the service's answers and keeps its existing order.

**Rollback:** point `latest` at an earlier commit tag and restart, or set `reranker.image.digest`.

The pinned requirements resolve for Python 3.12 on `linux/arm64` (257 MB of wheels) and `linux/amd64` (298 MB). The CI job `reranker-image` builds arm64 only, like the main image, because every mk4 node is arm64 and installing torch for amd64 under emulation added 12 minutes. It passed its smoke test, which starts it with `--network none`, `--read-only` and a non-root user.

## Measurements

All from an arm64 Mac with Python 3.11 and torch 2.15.0. **Linux CPU in a container will differ, so the Helm resources are provisional.** Reproduce with `python reranker/benchmark.py <model dir>`.

| Inference, 2 threads, batch 16 | p50 | p95 |
|---|---:|---:|
| 10 passages of 800 characters | 88 ms | 89 ms |
| 30 passages of 200 characters | 72 ms | 73 ms |
| 30 passages of 800 characters | 262 ms | 266 ms |
| 50 passages of 800 characters | 439 ms | 443 ms |
| 30 passages of 2000 characters | 891 ms | 1080 ms |

With 1 thread, 30 passages of 800 characters took 388 ms; with 4 threads, 211 ms. Passage length matters more than count, which is why `maxPassageChars` is a real control.

Model load took about 2.7 to 3.9 s, and the warm-up call about 0.1 s. Peak resident memory was about 480 MiB in a process with one model loaded. A running service was at 334 MiB after a burst of load.

Live against the real service, with 1 worker and 4 queue places: 12 simultaneous requests gave 5 answers and 7 `429` responses with `Retry-After`. Five requests with 120 ms deadlines all returned `504`, and the service stayed ready. `SIGTERM` stopped it cleanly, and no query or passage appeared in its log. Cluster CPU and memory under load are not measured.

## Evaluation

`moks eval rerank` compares the same questions and authorized candidates with reranking off and on, in the isolated evaluation realm, against a real service. Results from 2026-11-02 are in `docs/examples/rerank-evaluation-2026-11-02.json`.

| 30 cases, 29 documents | Off | On |
|---|---:|---:|
| nDCG@5 | 0.929 | 0.995 |
| MRR | 0.917 | 1.000 |
| Recall@3 | 0.944 | 0.982 |
| Recall@5 | 0.982 | 1.000 |
| Document diversity | 1.00 | 1.00 |
| End-to-end retrieval p50 | 2.7 ms | 35.0 ms |
| End-to-end retrieval p95 | 4.6 ms | 68.8 ms |
| Inference p50 / p95 | | 20.8 / 39.7 ms |

Every gate passed: no unauthorized document, citations unchanged, exact identifiers first, abstention unchanged, no fallback. Only 4 cases changed, all for the better: `k8s-exit137`, `k8s-503`, `k8s-names` and `argo-two-owners`. With the service stopped, the same run returned results identical to reranking off, and the rollout gates correctly said "keep disabled".

Read this as a reason to test further, not as proof:

- The dataset is small and synthetic, and the person who wrote the feature wrote it. Nothing was tuned on it, but these are not real operator questions.
- The candidates came from keyword search only. The evaluation realm has no vectors, and production uses them.
- The passages are short, so latency here is much lower than the benchmark above with long passages.
- The `long_passage` case did not reach truncation: the chunker had already split that document. `truncated_passages` was 0. Truncation was checked separately against the live service and is reported correctly, but its effect on quality is unmeasured.
- The gates in `rerank.yaml` are initial values, not derived from anything.

## Configuration

Helm values are under `reranker`. `reranker.enabled` deploys the service, and `reranker.client.enabled` makes search use it, so it can run and be evaluated before anyone's results change.

| Setting | Default | Ceiling in code |
|---|---:|---:|
| `MOKS_RAG_RERANK_ENABLED` | false | |
| `MOKS_RAG_RERANK_URL`, `_MODEL`, `_REVISION` | empty; all three are required | |
| `MOKS_RAG_RERANK_CANDIDATES` | 30 | 50 |
| `MOKS_RAG_RERANK_RESULTS` | 12 | 50 |
| `MOKS_RAG_RERANK_TIMEOUT_MS` | 1500 | 5000 |
| `MOKS_RAG_RERANK_MAX_QUERY_CHARS` | 1000 | 2000 |
| `MOKS_RAG_RERANK_MAX_PASSAGE_CHARS` | 2000 | 8000 |

The service has its own `RERANKER_*` settings for threads, batch size, concurrency and queue, and its own ceilings. Falling back to the existing order is the only failure policy, so there is no setting for it.

## Security

- Bearer token from a Kubernetes Secret, checked in constant time. The service refuses to start without one. It is `MOKS_CRED_RERANKER_TOKEN`, the same convention as other credentials.
- `ClusterIP` only, with no Ingress or VirtualService. NetworkPolicy allows the API and worker on 8090, metrics from the configured namespaces, and no egress at all. On a mesh, an AuthorizationPolicy names the two service accounts.
- Non-root, read-only root filesystem, all capabilities dropped, seccomp, no ServiceAccount token, and no access to the database or to MOKS's Secrets.
- Requests cannot name a model, path or URL. Validation errors never echo input. Queries and passages are never logged, and neither appears in a metric label.
- There is no cache, so there is no cross-scope cache to get wrong.

NetworkPolicy enforcement on `mk4`'s kindnet is unverified, so there the token is the control that is known to hold.

## Capacity

A slot belongs to the computation. It is released when the work really ends, so a client that times out or disconnects frees nothing while the CPU is busy. Queued work past its deadline never starts, and a running request stops between batches. A running batch cannot be interrupted. On `SIGTERM` the service stops being ready, refuses new work and finishes what it accepted.

## Enabling

1. Apply Terraform for the `moks-reranker` ECR repository, and let CI build.
2. Create the Secret from `deploy/secrets/moks-reranker-secrets.example.yaml`, and put the same token in `moks-secrets`.
3. Set `reranker.enabled: true`. Move `deploy/argocd/minikube/optional/moks-reranker.yaml` to `apps/`, sync `moks-root`, `moks-foundation`, `moks-reranker`, and restart the API and worker for the new configuration.
4. Check the service under **Knowledge > Index health > Reranking**, and measure real CPU and memory.
5. Run `moks eval rerank` in a worker pod, and build a dataset from real questions before trusting the result.
6. Set `reranker.client.enabled: true` only if the gates pass. Watch `moks_rerank_calls_total` by outcome.

**Rollback:** set `reranker.client.enabled: false`, or just stop the service: search falls back on its own.

## Metrics

MOKS: `moks_rerank_calls_total{outcome}`, `moks_rerank_seconds`, `moks_rerank_candidates`, `moks_rerank_truncated_passages_total`.
Service, on port 9464: `reranker_requests_total{outcome}`, `reranker_queue_wait_seconds`, `reranker_inference_seconds`, `reranker_candidates`, `reranker_truncated_passages_total`, `reranker_slots_held`, `reranker_model_load_failures_total`.

The counters on the administration page cover one API process since it started. Worker searches appear only in Prometheus.
