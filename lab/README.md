# MOKS demo incident lab

Four reproducible incidents in namespace `moks-lab`, each with expected evidence and an
authorized fix. All images are multi-arch (arm64 works on mk4), every pod runs non-root with a
read-only root filesystem (`restricted` Pod Security), and the whole lab uses under 0.5 CPU and
400 MiB.

```sh
kubectl --context mk4 apply -f lab/00-namespace.yaml
kubectl --context mk4 apply -f lab/alloy-logs-role.yaml   # optional: Loki evidence (also add moks-lab to Alloy's namespaces)
```

Ask MOKS from **Investigate** with environment `mk4` and namespace `moks-lab`. The report's
**Suggested actions** (from skills) and **Propose as action plan** turn a remediation into a
reviewed plan: a second person approves it (mk4 is production), the executor applies it to
`moks-lab` (`executor.namespaces`), validates the rollout and restores the previous state if
validation fails. The `kubectl` commands below are the manual equivalent. Each fix should be followed by a fresh
investigation to validate recovery; MOKS never claims recovery itself.

Without an active LLM, reports are rule-based. With one, MOKS also plans extra queries (for
example, the storefront's logs in scenario 1).

---

## 1. Broken Service selector → 502

```sh
kubectl --context mk4 apply -f lab/01-broken-selector.yaml
```

The `checkout` Service selects `app=checkout-v2`, but its pods are labelled `app=checkout`.
The `storefront` nginx proxies to `checkout` and returns **502 Bad Gateway**, and the `traffic`
pod logs `{"status":"502"}` every 2 seconds.

**Ask:** "Why is checkout returning 502 errors?"

**Expected evidence:**

- `k8s_triage` finding **"Service checkout selector matches no pods"**, naming the near-miss pods
  `checkout-…` with labels `{app: checkout}`. The Service shows 0 ready endpoints.
- `k8s_services` / Explorer → services: `checkout` shows 0 endpoints and a near-miss.
- With an LLM: `k8s_pod_logs` of `traffic`/`storefront` showing 502s, cited as facts.

**Diagnosis:** the selector typo leaves no endpoints, so nginx gets connection failures.

**Authorized remediation:**

```sh
kubectl --context mk4 -n moks-lab patch service checkout --type merge -p '{"spec":{"selector":{"app":"checkout"}}}'
```

**Validate:** `kubectl -n moks-lab get endpointslices -l kubernetes.io/service-name=checkout`
shows 2 ready addresses, the `traffic` logs switch to `200`, and a new investigation has no
selector finding.

## 2. CrashLoopBackOff from invalid configuration

```sh
kubectl --context mk4 apply -f lab/02-crashloop-config.yaml
```

**Ask:** "Why is payments crashing?"

**Expected evidence:**

- Finding **"payments-… is in CrashLoopBackOff (N restarts)"** (last exit code 78).
- Rule follow-up `k8s_pod_logs previous=true`: `invalid PAYMENT_MODE=lve (expected live|sandbox)`.
- `k8s_describe_pod`: `envFrom: configMapRef payments-config`.

**Diagnosis:** ConfigMap `payments-config` has `PAYMENT_MODE=lve`.

**Authorized remediation:**

```sh
kubectl --context mk4 -n moks-lab patch configmap payments-config --type merge -p '{"data":{"PAYMENT_MODE":"sandbox"}}'
kubectl --context mk4 -n moks-lab rollout restart deploy/payments
```

**Validate:** the pod is Running and Ready with no new restarts; its logs show `"event":"ready"`.

## 3. OOMKilled workload

```sh
kubectl --context mk4 apply -f lab/03-oomkilled.yaml
```

**Ask:** "Which pods were OOMKilled and how close were they to their memory limits?"

**Expected evidence:**

- Finding **"cache-… container cache was OOMKilled"** (exit 137, restarting).
- `k8s_describe_pod`: `limits.memory: 64Mi`.
- `prom_pod_resources`: working set climbing to the limit before each restart
  (`container_memory_working_set_bytes` vs `container_spec_memory_limit_bytes`).
- Logs: `loading 160 MiB working set`.

**Diagnosis:** the working set (~160 MiB) exceeds the 64 MiB limit.

**Authorized remediation** (reviewed configuration change):

```sh
kubectl --context mk4 -n moks-lab set resources deploy/cache --limits=memory=256Mi --requests=memory=192Mi
```

**Validate:** the pod logs `"event":"ready"`, restarts stop, and memory plateaus below the new limit.

## 4. Latency correlated with a dependency slowdown

```sh
kubectl --context mk4 apply -f lab/04-latency.yaml
# wait ~2 minutes for a healthy baseline, then introduce the regression as a rollout:
kubectl --context mk4 -n moks-lab set env deploy/inventory DELAY_MS=1500
```

`orders` calls `inventory` every second and logs `durationMs`. The second command creates a new
`inventory` ReplicaSet revision, which is the "deployment" to correlate with.

**Ask:** "Correlate increased latency of orders with deployments and application logs in the last 30 minutes."

**Expected evidence:**

- `k8s_rollout_history inventory`: revision 2 created at T, with the pod template changed
  (images unchanged). This becomes the **recent_rollout** finding: timing correlation only,
  explicitly not proof.
- `k8s_pod_logs orders`: `durationMs` jumps from about 20–40 to about 1,500, at level `warn`,
  after T.
- Optional metrics: add a scrape job for `orders-metrics.moks-lab.svc:9100` (DNS SD, as for
  MOKS) and query
  `histogram_quantile(0.95, rate(lab_order_duration_seconds_bucket[1m]))`.

**Diagnosis:** the inventory rollout at T added ~1.5 s per call, and orders latency follows it.

**Authorized remediation:** roll back the dependency to the previous revision:

```sh
kubectl --context mk4 -n moks-lab rollout undo deploy/inventory
```

**Validate:** `durationMs` returns to baseline in the `orders` logs, and the rollout history
shows revision 3 (= revision 1's template).

---

## Cleanup

```sh
kubectl --context mk4 delete namespace moks-lab
```

## Verified so far

The manifests pass `kubectl apply --dry-run=client`. The detectors for scenarios 1–3 are covered
by unit and integration tests against the same object shapes (`backend/tests`). **The lab has not
yet been applied to mk4.** Run it, then record the results in [../docs/TESTING.md](../docs/TESTING.md).
