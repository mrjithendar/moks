#!/usr/bin/env bash
# Run one step of docs/OPENSEARCH.md against moks-search, as a Job made from the suspended CronJob
# moks-search-ops. Each step waits, prints the Job's output and exits with its result.
#
#   scripts/opensearch-migrate.sh status
#   scripts/opensearch-migrate.sh build          fill a generation from PostgreSQL (costs embedding calls)
#   scripts/opensearch-migrate.sh sync           write what that generation has missed since
#   scripts/opensearch-migrate.sh validate       exits 1 unless every check passes
#   scripts/opensearch-migrate.sh compare        keyword overlap with the old cluster, ids and counts only
#   scripts/opensearch-migrate.sh activate NAME  switch back to a retained generation
#   scripts/opensearch-migrate.sh snapshots --bucket NAME
#
# CONTEXT sets the kube context (default mk4).
set -euo pipefail
CONTEXT="${CONTEXT:-mk4}"
[[ $# -ge 1 ]] || { sed -n '2,13p' "$0"; exit 2; }
k() { kubectl --context "$CONTEXT" -n moks "$@"; }
JOB="moks-search-$1-$(date +%s)"
ARGS="$(printf '%s\n' opensearch "$@" | jq -R . | jq -s .)"
k get cronjob moks-search-ops -o json | jq --arg name "$JOB" --argjson args "$ARGS" '{
  apiVersion: "batch/v1", kind: "Job",
  metadata: {name: $name, namespace: "moks", labels: {app: "moks", component: "search-ops"}},
  spec: (.spec.jobTemplate.spec | .template.spec.containers[0].args = $args)}' | k create -f -
k wait --for=condition=Ready pod -l "job-name=$JOB" --timeout=300s > /dev/null || true
k logs -f "job/$JOB" || true
if [[ "$(k get job "$JOB" -o jsonpath='{.status.succeeded}')" != 1 ]]; then
  for _ in 1 2 3 4 5; do
    [[ "$(k get job "$JOB" -o jsonpath='{.status.succeeded}')" == 1 ]] && exit 0
    sleep 2
  done
  echo "STOP: $JOB did not succeed" >&2
  exit 1
fi
