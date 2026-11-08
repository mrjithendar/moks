#!/usr/bin/env bash
# Finish turning the web terminal on after Argo CD has synced terminal.enabled: true, and check it.
# Safe to rerun: each step does nothing if it is already done (docs/TERMINAL.md).
#
#   scripts/enable-terminal.sh [email] [kube-context] [environment]
#
# Then open a session in the portal and run moks-verify-isolation inside it.
set -euo pipefail
EMAIL="${1:-team@jithendar.com}"
CONTEXT="${2:-mk4}"
ENVIRONMENT="${3:-$CONTEXT}"
TERMINALS=moks-terminals
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
kc() { kubectl --context "$CONTEXT" "$@"; }
k() { kc -n moks "$@"; }

k get deploy moks-terminal >/dev/null || { echo "Deployment moks-terminal missing: set terminal.enabled, then sync moks-terminal." >&2; exit 1; }
kc get namespace "$TERMINALS" >/dev/null

if [[ -z "$(k get secret moks-terminal-secrets --ignore-not-found -o name)" ]]; then
  "$HERE/create-terminal-secrets.sh" "$CONTEXT"
else
  echo "→ moks-terminal-secrets is in place"
fi
k rollout status deploy/moks-terminal --timeout=180s

# Session pods pull moks-terminal:latest from Harbor with harbor-pull (miniKube deploy/minikube/harbor/registry.py).
if [[ -z "$(kc -n "$TERMINALS" get secret harbor-pull --ignore-not-found -o name)" ]]; then
  python3 "${MINIKUBE_DIR:-$(dirname "$(dirname "$HERE")")/miniKube}/deploy/minikube/harbor/registry.py" pull-secrets --context "$CONTEXT" --namespace "$TERMINALS"
  kc -n "$TERMINALS" get secret harbor-pull >/dev/null
fi
echo "→ harbor-pull is in place"

# terminal.use belongs to the terminal role alone, so admins need this too. Without it the page has no
# "Open a terminal" form. The API image holds the moks CLI and the database URL it needs.
k exec deploy/moks-api -c api -- moks grant "$EMAIL" "terminal:$ENVIRONMENT:*"
echo "→ $EMAIL holds terminal on $ENVIRONMENT"

echo "Done. Reload the Terminal page, open a session, and run moks-verify-isolation inside it."
