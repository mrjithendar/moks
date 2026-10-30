#!/usr/bin/env bash
# Run MOKS locally against a cluster: API + embedded worker + built UI at http://localhost:8080/moks/
#   Prereqs: PostgreSQL reachable at $MOKS_DATABASE_URL, `npm run build` in frontend/,
#   a kubeconfig at $MOKS_CREDENTIALS_DIR/kubeconfig, and port-forwards:
#     kubectl -n monitoring port-forward svc/prometheus 19090:9090 &
#     kubectl -n monitoring port-forward svc/loki 13100:3100 &
set -euo pipefail
cd "$(dirname "$0")/.."
export MOKS_ENV=development MOKS_AUTH_MODE=dev MOKS_COOKIE_SECURE=false
export MOKS_PUBLIC_URL="${MOKS_PUBLIC_URL:-http://localhost:8080/moks}"
export MOKS_DATABASE_URL="${MOKS_DATABASE_URL:-postgresql+asyncpg://moks@localhost:5432/moks}"
export MOKS_STATIC_DIR="$PWD/frontend/dist" MOKS_EMBEDDED_WORKER=true MOKS_API_DOCS=true
export MOKS_BOOTSTRAP_ADMINS="${MOKS_BOOTSTRAP_ADMINS:-sre@moks.local}"
export MOKS_CREDENTIALS_DIR="${MOKS_CREDENTIALS_DIR:-$HOME/.moks-dev}"
export MOKS_METRICS_PORT="${MOKS_METRICS_PORT:-9464}"
cd backend
python -m moks.cli migrate
python -m moks.cli seed ../dev/seed.local.yaml
exec python -m moks.cli serve
