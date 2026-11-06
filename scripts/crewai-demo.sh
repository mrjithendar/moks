#!/usr/bin/env bash
# CrewAI smoke-test lab (docs/crewai-investigation.md, section "Smoke test").
#   scripts/crewai-demo.sh up      create namespace crewai-demo with two unhealthy workloads
#   scripts/crewai-demo.sh status  show what MOKS should find
#   scripts/crewai-demo.sh down    delete the namespace and everything in it
# KUBE_CONTEXT picks the cluster (default mk4). Nothing outside namespace crewai-demo is touched.
set -euo pipefail
CONTEXT="${KUBE_CONTEXT:-mk4}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
k() { kubectl --context "$CONTEXT" "$@"; }

case "${1:-}" in
  up)
    k apply -f "$HERE/lab/crewai-demo/demo.yaml"
    echo "Waiting up to 90s for the failures to show..."
    for _ in $(seq 1 18); do
      reasons="$(k -n crewai-demo get pods -o jsonpath='{range .items[*]}{.status.containerStatuses[0].state.waiting.reason}{" "}{end}' 2>/dev/null || true)"
      case "$reasons" in *ImagePull*CrashLoopBackOff*|*CrashLoopBackOff*ImagePull*) break ;; esac
      sleep 5
    done
    k -n crewai-demo get pods -o wide
    ;;
  status)
    k -n crewai-demo get deploy,pods
    k -n crewai-demo get events --sort-by=.lastTimestamp | tail -15
    ;;
  down)
    k delete namespace crewai-demo --ignore-not-found --wait=true
    ;;
  *)
    echo "usage: $0 up|status|down" >&2
    exit 2
    ;;
esac
