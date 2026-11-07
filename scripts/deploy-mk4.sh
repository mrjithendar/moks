#!/usr/bin/env bash
# Run MOKS on mk4 from origin/master, onto the data it kept, and retire the EKS Applications.
# This is "mk4 with no running portal" in docs/ARGOCD_APP_SPLIT.md, in order. Safe to rerun; it never
# prunes, and nothing it does deletes a PVC or a workload.
#
#   scripts/deploy-mk4.sh --check     read-only preflight
#   scripts/deploy-mk4.sh             preflight, then deploy
#
# BACKUP_DIR sets where the pre-migration database dump goes (default ~/moks-mk4-backup).
set -euo pipefail
MODE="${1:-}"
[[ -z "$MODE" || "$MODE" == --check ]] || { echo "usage: $0 [--check]" >&2; exit 2; }
CONTEXT=mk4
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
TOOLS="$(dirname "$REPO")/tools"
BACKUP_DIR="${BACKUP_DIR:-$HOME/moks-mk4-backup}"
EKS_PREFIX=roboshop-demo-moks
kc() { kubectl --context "$CONTEXT" "$@"; }
die() { echo "STOP: $*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }

sync() {
  argocd app get "moks-$1" --hard-refresh > /dev/null
  argocd app sync "moks-$1" --timeout 600        # never add --prune
  argocd app wait "moks-$1" --sync --health --timeout 600
}

investigations() {
  kc -n moks exec statefulset/moks-postgres -c postgres -- psql -U moks -d moks -tAc 'select count(*) from investigations'
}

step "Preflight (read-only)"
kc get node mk4-m04 -o name > /dev/null
argocd app list -o name > /dev/null || die "argocd is not logged in; run: argocd login jithendar.com --grpc-web --grpc-web-root-path /argocd --username admin"
git -C "$REPO" fetch -q origin
git -C "$REPO" diff --quiet origin/master -- deploy/helm/moks deploy/argocd/minikube deploy/argocd/project-root.yaml \
  || die "Argo CD reads origin/master, which differs from this checkout. Push first."
for secret in moks-secrets moks-postgres moks-terminal-secrets; do
  kc -n moks get secret "$secret" -o name > /dev/null
done
kc -n moks get pvc data-moks-postgres-0 -o name > /dev/null || die "the Postgres claim is gone; this is no longer a redeploy onto kept data"
[[ -f "$TOOLS/deploy/minikube/harbor/registry.py" ]] || die "no tools checkout at $TOOLS (its Harbor registry.py writes harbor-pull)"
curl -fsS -o /dev/null https://registry.jithendar.com/api/v2.0/ping || die "Harbor at registry.jithendar.com does not answer; every MOKS image comes from it"
finalizers="$(kc -n argocd get applications -o json \
  | jq -r --arg p "$EKS_PREFIX-" '.items[] | select(.metadata.name | startswith($p)) | select(.metadata.finalizers // [] | length > 0) | .metadata.name')"
[[ -z "$finalizers" ]] || die "these Applications have finalizers, so deleting them would cascade: $finalizers"
echo "ok"
[[ "$MODE" == --check ]] && exit 0

step "Keep the Postgres volume if its claim is ever deleted"
volume="$(kc -n moks get pvc data-moks-postgres-0 -o jsonpath='{.spec.volumeName}')"
kc patch pv "$volume" -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'

step "Retire the EKS Applications (the cluster is gone, so these only remove Argo CD objects)"
kc -n argocd delete application --ignore-not-found "$EKS_PREFIX-root"
for group in foundation database api worker executor routing terminal migrate opensearch backup; do
  kc -n argocd delete application --ignore-not-found "$EKS_PREFIX-$group"
done
kc -n argocd delete appproject --ignore-not-found moks-eks

step "Namespaces the chart writes into"
kc apply -f "$REPO/lab/00-namespace.yaml"

step "Harbor pull secrets: every MOKS image comes from registry.jithendar.com/jithendar"
# harbor-pull (robot$k8s-pull) never expires, so this only fills namespaces that lack it. registry.py asks for
# the robot's secret when the robot already exists.
pull_secrets() {
  local missing=()
  for ns in "$@"; do
    kc get namespace "$ns" -o name > /dev/null 2>&1 || continue
    kc -n "$ns" get secret harbor-pull -o name > /dev/null 2>&1 || missing+=(--namespace "$ns")
  done
  [[ ${#missing[@]} -eq 0 ]] || python3 "$TOOLS/deploy/minikube/harbor/registry.py" pull-secrets --context "$CONTEXT" "${missing[@]}"
}
pull_secrets moks

step "Clear the leftover moks-portal tracking on Namespace/velero"
tracked="$(mktemp)"
trap 'rm -f "$tracked"' EXIT
echo '[{"group":"","kind":"Namespace","name":"velero"}]' > "$tracked"
"$HERE/argocd-clear-tracking.sh" "$CONTEXT" moks-portal "$tracked" --apply

step "Projects and root"
kc apply -f "$REPO/deploy/argocd/project-root.yaml" -f "$REPO/deploy/argocd/minikube/project.yaml" -f "$REPO/deploy/argocd/minikube/root.yaml"
argocd app get moks-root --hard-refresh > /dev/null
argocd app sync moks-root --timeout 300

step "Foundation and database"
sync foundation
sync database
kc -n moks rollout status statefulset/moks-postgres --timeout=300s
[[ "$(kc -n moks get pvc data-moks-postgres-0 -o jsonpath='{.spec.volumeName}')" == "$volume" ]] \
  || die "data-moks-postgres-0 moved off $volume"

step "Dump the database before the first API pod migrates it"
before="$(investigations)"
echo "investigations: $before"
(umask 077; mkdir -p "$BACKUP_DIR")
"$HERE/backup.sh" "$CONTEXT" "$BACKUP_DIR"
dump="$(ls -t "$BACKUP_DIR"/moks-*.sql.gz | head -1)"
gzip -t "$dump"

step "Application components"
for group in api worker executor terminal routing; do
  sync "$group"
done

step "Harbor pull secrets again: the terminal and diagnostics namespaces only exist now"
pull_secrets moks-terminals moks-diagnostics

step "Verify"
after="$(investigations)"
echo "investigations: $before before, $after after"
[[ "$after" == "$before" ]] || die "investigation count changed; the dump is $dump"
curl -fsS https://jithendar.com/moks/api/health
echo
argocd app list -p moks
