#!/usr/bin/env bash
# Deploy MOKS to mk4 (Hetzner k3s) with Argo CD, from origin/master. Safe to rerun; it never prunes.
#
#   scripts/deploy-hetzner.sh --check     read-only preflight
#   scripts/deploy-hetzner.sh             preflight, then apply the projects and the root and wait for the apps
#
# The root (deploy/argocd/hetzner/root.yaml) writes one Application per component from deploy/argocd/hetzner/apps;
# each auto-syncs (selfHeal, never prune). Only kubectl is used, so no argocd CLI login is needed.
# Before the first run: scripts/create-secrets-hetzner.sh.
set -euo pipefail
MODE="${1:-}"
[[ -z "$MODE" || "$MODE" == --check ]] || { echo "usage: $0 [--check]" >&2; exit 2; }
CONTEXT="${KUBE_CONTEXT:-mk4}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
kc() { kubectl --context "$CONTEXT" "$@"; }
die() { echo "STOP: $*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }

step "Preflight (read-only)"
kc -n argocd get deploy argocd-server -o name > /dev/null || die "no Argo CD in namespace argocd on $CONTEXT"
kc -n postgres-db get clusters.postgresql.cnpg.io postgres-cluster -o name > /dev/null || die "no CloudNativePG cluster postgres-db/postgres-cluster"
git -C "$REPO" fetch -q origin
git -C "$REPO" diff --quiet origin/master -- deploy/helm/moks deploy/argocd/hetzner deploy/argocd/project-root.yaml \
  || die "Argo CD reads origin/master, which differs from this checkout. Push first."
missing=()
for secret in harbor-pull postgres-db-credentials moks-secrets moks-reranker-secrets moks-terminal-secrets moks-secret-sync; do
  kc -n moks get secret "$secret" -o name > /dev/null 2>&1 || missing+=("$secret")
done
[[ ${#missing[@]} -eq 0 ]] || die "missing Secrets in moks: ${missing[*]} (scripts/create-secrets-hetzner.sh; harbor-pull from tools registry.py pull-secrets)"
curl -fsS -o /dev/null https://registry.jithendar.com/api/v2.0/ping || die "Harbor at registry.jithendar.com does not answer; every MOKS image comes from it"
echo ok
[[ "$MODE" == --check ]] && exit 0

step "Namespaces the chart writes into"
kc apply -f "$REPO/lab/00-namespace.yaml"

step "Projects and root"
kc apply -f "$REPO/deploy/argocd/project-root.yaml" -f "$REPO/deploy/argocd/hetzner/project.yaml" -f "$REPO/deploy/argocd/hetzner/root.yaml"
kc -n argocd annotate application moks-root argocd.argoproj.io/refresh=hard --overwrite > /dev/null

step "Retire child Applications that moved to optional/ (the root never prunes them)"
# moks-database renders nothing with postgres.bundled false. Without a finalizer, deleting the Application
# leaves whatever it once managed in place.
for app in $(cd "$REPO/deploy/argocd/hetzner/optional" && ls *.yaml | sed 's/\.yaml$//'); do
  kc -n argocd get application "$app" -o name > /dev/null 2>&1 || continue
  [[ -z "$(kc -n argocd get application "$app" -o jsonpath='{.metadata.finalizers}')" ]] || die "$app has finalizers; deleting it would cascade"
  [[ -z "$(kc -n argocd get application "$app" -o jsonpath='{.status.resources}')" ]] || { echo "$app still manages resources; left alone"; continue; }
  kc -n argocd delete application "$app"
done

step "Wait for every child Application to be Synced and Healthy"
apps=$(cd "$REPO/deploy/argocd/hetzner/apps" && ls *.yaml | sed 's/\.yaml$//')
for i in $(seq 1 60); do
  pending=()
  for app in $apps; do
    s="$(kc -n argocd get application "$app" -o jsonpath='{.status.sync.status}/{.status.health.status}' 2>/dev/null || true)"
    [[ "$s" == Synced/Healthy ]] || pending+=("$app=${s:-absent}")
  done
  [[ ${#pending[@]} -eq 0 ]] && break
  (( i % 6 == 0 )) && echo "waiting: ${pending[*]}"
  sleep 10
done
kc -n argocd get applications | grep -E '^NAME|^moks-'
[[ ${#pending[@]} -eq 0 ]] || die "not Synced/Healthy after 10 minutes: ${pending[*]}"

step "Verify"
curl -fsS https://jithendar.com/moks/api/health
echo
