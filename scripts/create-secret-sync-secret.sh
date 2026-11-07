#!/usr/bin/env bash
# Create the executor-only Secret for secret sync (docs/SECRETS_SYNC.md) in namespace moks.
#   scripts/create-secret-sync-secret.sh [kube-context]           # Minikube: prompts for the IAM access keys
#   NO_AWS_KEYS=1 scripts/create-secret-sync-secret.sh [context]   # EKS: Pod Identity, digest key only
# Refuses to overwrite: a new digest key invalidates every mapping's baseline. Nothing is echoed or written to disk.
set -euo pipefail
CONTEXT="${1:-mk4}"
NS=moks
NAME=moks-secret-sync
k() { kubectl --context "$CONTEXT" "$@"; }

if k -n "$NS" get secret "$NAME" >/dev/null 2>&1; then
  echo "Secret $NS/$NAME exists; leaving it unchanged." >&2
  exit 1
fi
digest="$(openssl rand -base64 48 | tr -d '\n')"
key_id="" secret_key=""
if [ -z "${NO_AWS_KEYS:-}" ]; then
  read -r -s -p "AWS access key id for secret sync: " key_id; echo
  read -r -s -p "AWS secret access key: " secret_key; echo
  [ -n "$key_id" ] && [ -n "$secret_key" ] || { echo "Both keys are needed (or set NO_AWS_KEYS=1)." >&2; exit 1; }
fi
# values go through a pipe from builtins, never through argv, where ps would show them
k -n "$NS" create secret generic "$NAME" --from-env-file=<(
  printf 'MOKS_SECRETS_DIGEST_KEY=%s\n' "$digest"
  if [ -n "$key_id" ]; then
    printf 'MOKS_CRED_SECRETS_AWS_ACCESS_KEY_ID=%s\nMOKS_CRED_SECRETS_AWS_SECRET_ACCESS_KEY=%s\n' "$key_id" "$secret_key"
  fi
) >/dev/null
unset digest key_id secret_key
echo "Created $NS/$NAME. Restart the executor so it loads it:"
echo "  kubectl --context $CONTEXT -n $NS rollout restart deploy/moks-executor"
