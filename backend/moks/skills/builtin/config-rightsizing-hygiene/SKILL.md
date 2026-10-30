---
name: config-rightsizing-hygiene
description: "Review a namespace's workload configuration end to end: right-size requests and limits from measured usage, and find unsafe or fragile settings in probes, images, security context, ConfigMap and Secret usage; propose concrete, reviewable changes."
version: "1"
triggers:
  keywords: [rightsizing, right-size, rightsize, provision, oversized, undersized, requests, limits, resources, configuration, config, configmap, secret, secrets, hardening, best practice, best practices, tune, tuning, optimize, optimise, misconfiguration, review, audit, hygiene, overprovisioned, underprovisioned, waste]
  findings: [memory_near_limit, under_provisioned, over_provisioned, config_ref_missing, config_error, plaintext_secret_env, missing_requests, missing_memory_limit, mutable_image_tag, privileged_container, host_path_volume, missing_readiness_probe]
steps:
  - tool: finops_rightsizing
    args: {namespace: "{namespace}", days: 7}
    purpose: requests and limits versus 7 days of measured p95 CPU and peak memory
  - tool: k8s_config_audit
    args: {namespace: "{namespace}"}
    purpose: probes, image pinning, security context, Secret/ConfigMap references (contents never read)
  - tool: k8s_workloads
    args: {namespace: "{namespace}"}
    purpose: replicas and rollout state before proposing changes
  - tool: k8s_events
    args: {namespace: "{namespace}", warnings_only: true}
    purpose: missing ConfigMap/Secret keys, failed mounts, OOM and probe failures
  - tool: prom_pod_resources
    args: {namespace: "{namespace}"}
    when_question: "limit|memory|cpu|throttl|oom|rightsiz|size"
    purpose: usage curves behind the recommendations
  - tool: kb_search
    args: {query: "{namespace} configuration resources secrets runbook"}
    purpose: documented sizing or configuration decisions
remediation:
  - when: memory_near_limit
    action: patch_resources
    note: "Raise memory request/limit to measured peak + 25% before it OOMs; validate the rollout."
  - when: under_provisioned
    action: patch_resources
    note: "Raise the CPU request to measured p95 + 25% to stop throttling and noisy-neighbour risk."
  - when: over_provisioned
    action: patch_resources
    note: "Reduce requests to measured usage + 25% headroom; apply one workload at a time and watch for throttling/OOM."
  - when: plaintext_secret_env
    action: open_pull_request
    note: "Move the literal value into a Secret (or External Secrets) and reference it with secretKeyRef; rotate the exposed value. MOKS never writes Secret values."
  - when: config_ref_missing
    action: open_pull_request
    note: "Restore the missing ConfigMap/Secret or key in Git (or fix the reference); do not create it by hand in the cluster."
  - when: missing_readiness_probe
    action: open_pull_request
    note: "Add a readiness probe on the app's health endpoint in the manifest/Helm values."
  - when: mutable_image_tag
    action: open_pull_request
    note: "Pin the image to a version tag or digest in the manifest."
---
# Configuration and rightsizing review

Work through the namespace in this order, and keep every recommendation tied to evidence.

## 1. Safety first, then savings
Fix **risks** before **savings**:
1. `memory_near_limit` (peak within 10% of the limit) and `under_provisioned` (CPU p95 above the request).
   These cause OOMKills and throttling under the next load spike.
2. `config_ref_missing` and `config_error`: pods that cannot start or start without configuration.
3. `plaintext_secret_env`, `privileged_container`, `host_path_volume`: security exposure.
4. Only then `over_provisioned` savings.

## 2. Rightsizing method
- Window: 7 days by default; say if less data exists (new workloads, recent restarts).
- CPU request = p95 × 1.25. Memory request = peak working set × 1.25; memory limit ≥ request (MOKS
  suggests 1.5× for at-risk containers). Never recommend below the observed peak.
- Usage that **grows without bound** (leak) is not a sizing problem; say so and do not just raise limits.
- JVM, Node and Python runtimes need heap settings that match the new memory limit; mention it.
- Batch or bursty workloads: p95 can understate peaks; prefer the peak and say why.
- One change per workload, applied through a `patch_resources` action plan (reviewed, approved in
  production, rollout validated, automatically reverted on failure).
- Report the monthly saving or cost increase with the cost model it came from, or say the cost model has no price.

## 3. Configuration hygiene
- **Requests/limits missing:** BestEffort pods are evicted first and cannot be scheduled reliably.
- **Probes:** readiness protects traffic; liveness restarts hung processes. Do not add a liveness probe
  that calls downstream services (it turns a dependency outage into a restart storm).
- **Images:** `:latest` or untagged images make rollouts unreproducible and rollback meaningless; pin versions or digests.
- **Security context:** privileged, root and privilege escalation are only acceptable with a written reason.

## 4. ConfigMaps and Secrets (evidence without reading them)
MOKS cannot and must not read Secret or ConfigMap contents. Reason only from:
- **references** in pod specs (`envFrom`, `secretKeyRef`, `configMapKeyRef`, volumes);
- **events** (`secret "x" not found`, `couldn't find key k in ConfigMap y`);
- **literal env values whose names look sensitive** (`*_PASSWORD`, `*_TOKEN`, `API_KEY`): the value is
  never shown; recommend moving it to a Secret **and rotating it**, since it has been visible in the pod spec.

Recommend:
- Secrets mounted as files or referenced per key rather than `envFrom` of a whole Secret.
- `optional: true` only for genuinely optional settings.
- Changes through Git (`open_pull_request`) or an external secret manager, never live edits.
- Roll the Deployment after a ConfigMap change, because env vars do not update in running pods.

## Output
- Group the findings: **risks** (fix now), **configuration** (fix soon), **savings** (optional).
- Give each one the workload, container, current → recommended values, and the evidence reference.
