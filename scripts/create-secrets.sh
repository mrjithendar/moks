#!/usr/bin/env bash
# Create the MOKS Secrets in namespace moks with freshly generated values.
#   scripts/create-secrets.sh [kube-context]
# Refuses to overwrite existing Secrets: rotating the PostgreSQL password needs ALTER ROLE too.
set -euo pipefail
CONTEXT="${1:-mk4}"
NS=moks
k() { kubectl --context "$CONTEXT" "$@"; }

k get namespace "$NS" >/dev/null 2>&1 || k create namespace "$NS"
for s in moks-secrets moks-postgres; do
  if k -n "$NS" get secret "$s" >/dev/null 2>&1; then
    echo "Secret $NS/$s exists; leaving it unchanged." >&2
    exit 1
  fi
done
PG_PASSWORD="$(openssl rand -hex 24)"
SESSION_SECRET="$(openssl rand -base64 48 | tr -d '\n')"
k -n "$NS" create secret generic moks-postgres --from-literal=password="$PG_PASSWORD"
k -n "$NS" create secret generic moks-secrets \
  --from-literal=MOKS_DATABASE_URL="postgresql+asyncpg://moks:${PG_PASSWORD}@moks-postgres:5432/moks" \
  --from-literal=MOKS_SESSION_SECRET="$SESSION_SECRET"
echo "Created $NS/moks-postgres and $NS/moks-secrets. Add MOKS_OIDC_CLIENT_SECRET / MOKS_CRED_* keys with:"
echo "  kubectl --context $CONTEXT -n $NS patch secret moks-secrets --type merge -p '{\"stringData\":{\"MOKS_CRED_OPENAI_API_KEY\":\"...\"}}'"
