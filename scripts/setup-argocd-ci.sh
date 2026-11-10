#!/usr/bin/env bash
# One-time: lets CI deploy MOKS through Argo CD. CI builds and pushes moks:latest; its deploy job then asks Argo CD
# to sync the MOKS Applications in order and restart the Deployments that run the image. CI holds no Kubernetes
# credentials, only a token for Argo CD project role moks/ci (deploy/argocd/hetzner/project.yaml): get, sync and
# restart Applications of project moks.
#
#   scripts/setup-argocd-ci.sh             apply the project (adds the role), issue a token, store it in GitHub
#   SKIP_GH=1 scripts/setup-argocd-ci.sh   same, but print what to set in GitHub instead of setting it
#   ROTATE=1 scripts/setup-argocd-ci.sh    revoke the role's existing tokens first
#
# Stores: secret ARGOCD_AUTH_TOKEN, variable ARGOCD_SERVER (jithendar.com) and variable MK4_DEPLOY=true.
# Needs: kubectl with admin on context mk4 (the root Application does not manage AppProjects, so the project is
# applied here), argocd CLI logged in as admin, gh signed in with admin on the repository (unless SKIP_GH=1).
# TOKEN_EXPIRY sets the token lifetime (default 365d); rerun with ROTATE=1 before it lapses.
set -euo pipefail

REPO="${GITHUB_REPO:-mrjithendar/moks}"
CONTEXT="${KUBE_CONTEXT:-mk4}"
SERVER="${ARGOCD_SERVER:-jithendar.com}"
EXPIRY="${TOKEN_EXPIRY:-365d}"
ARGO=(--server "$SERVER" --grpc-web --grpc-web-root-path /argocd)
cd "$(dirname -- "${BASH_SOURCE[0]}")/.."

echo "→ Applying deploy/argocd/hetzner/project.yaml (adds role ci)"
kubectl --context "$CONTEXT" apply -f deploy/argocd/hetzner/project.yaml

if [[ "${ROTATE:-0}" == "1" ]]; then
  echo "→ Revoking existing tokens of moks/ci"
  for id in $(argocd "${ARGO[@]}" proj role get moks ci -o json | jq -r '.jwtTokens[]?.id // empty'); do
    argocd "${ARGO[@]}" proj role delete-token moks ci "$id"
  done
fi

echo "→ Issuing a token for project role moks/ci (expires in $EXPIRY)"
TOKEN="$(argocd "${ARGO[@]}" proj role create-token moks ci --expires-in "$EXPIRY" --token-only)"
[[ -n "$TOKEN" ]] || { echo "No token was issued." >&2; exit 1; }

echo "→ Testing the token: it must see moks-api and must not see moks-root"
argocd --auth-token "$TOKEN" "${ARGO[@]}" app get moks-api -o name > /dev/null
if argocd --auth-token "$TOKEN" "${ARGO[@]}" app get moks-root -o name > /dev/null 2>&1; then
  echo "The CI token can read moks-root; it must not." >&2; exit 1
fi

if [[ "${SKIP_GH:-0}" == "1" ]]; then
  printf '%s' "$TOKEN" | pbcopy
  unset TOKEN
  echo "Set in $REPO → Settings → Secrets and variables → Actions:"
  echo "  Secret   ARGOCD_AUTH_TOKEN = the token, now on the clipboard (it is not printed)"
  echo "  Variable ARGOCD_SERVER     = $SERVER"
  echo "  Variable MK4_DEPLOY        = true"
  exit 0
fi
printf '%s' "$TOKEN" | gh secret set ARGOCD_AUTH_TOKEN --repo "$REPO"
unset TOKEN
gh variable set ARGOCD_SERVER --repo "$REPO" --body "$SERVER"
gh variable set MK4_DEPLOY --repo "$REPO" --body true
echo "Done: every master build of $REPO now deploys to mk4 through Argo CD."
