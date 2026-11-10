#!/usr/bin/env bash
# Create the MOKS Secrets on mk4 (Hetzner k3s), where the database is the shared CloudNativePG cluster.
#   scripts/create-secrets-hetzner.sh [kube-context]
#
# MOKS_DATABASE_URL is built from Secret moks/postgres-db-credentials (written by miniKube
# minikube-infrastructure/postgres.tf), through the session-mode pooler. Keys in the gitignored
# moks-secrets.yaml / moks-terminal-secrets.yaml beside the repository root (MOKS_CRED_*, MOKS_SESSION_SECRET,
# the terminal's AWS keys) are carried over when those files exist; otherwise a new session secret is made.
# Creates moks-secrets, moks-terminal-secrets (when the file exists), moks-reranker-secrets and moks-secret-sync.
# Refuses to overwrite a Secret that exists. Prints no values; they reach kubectl on stdin, never argv.
set -euo pipefail
CONTEXT="${1:-mk4}"
NS=moks
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
k() { kubectl --context "$CONTEXT" -n "$NS" "$@"; }

k get secret postgres-db-credentials -o name > /dev/null \
  || { echo "Secret $NS/postgres-db-credentials is missing: apply miniKube minikube-infrastructure (postgres.tf) first." >&2; exit 1; }
for s in moks-secrets moks-terminal-secrets moks-reranker-secrets; do
  if k get secret "$s" -o name > /dev/null 2>&1; then
    echo "Secret $NS/$s exists; leaving everything unchanged." >&2
    exit 1
  fi
done

k get secret postgres-db-credentials -o json | python3 <(cat <<'PY'
import base64, json, secrets, sys, urllib.parse
from pathlib import Path
import yaml
repo = Path(sys.argv[1])
c = {k: base64.b64decode(v).decode() for k, v in json.load(sys.stdin)["data"].items()}
q = lambda v: urllib.parse.quote(v, safe="")
url = f"postgresql+asyncpg://{q(c['username'])}:{q(c['password'])}@{c['host']}:{c['port']}/{c['dbname']}"
token = secrets.token_urlsafe(32)   # docs/RERANKING.md: the same token in moks-secrets and moks-reranker-secrets

def saved(name):
    f = repo / f"{name}.yaml"
    if not f.is_file():
        return None
    d = yaml.safe_load(f.read_text())
    data = {k: base64.b64decode(v).decode() for k, v in (d.get("data") or {}).items()}
    data.update(d.get("stringData") or {})
    return data

def secret(name, data):
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "metadata": {"name": name}, "stringData": data}

main = saved("moks-secrets") or {}
main.setdefault("MOKS_SESSION_SECRET", secrets.token_urlsafe(48))
main.update(MOKS_DATABASE_URL=url, MOKS_CRED_RERANKER_TOKEN=token)
out = [secret("moks-secrets", main), secret("moks-reranker-secrets", {"MOKS_CRED_RERANKER_TOKEN": token})]
terminal = saved("moks-terminal-secrets")
if terminal is not None:
    # The shared cluster has no moks_terminal role, so the gateway uses the moks role (docs/TERMINAL.md).
    terminal.update(MOKS_DATABASE_URL=url, MOKS_SESSION_SECRET=main["MOKS_SESSION_SECRET"])
    out.append(secret("moks-terminal-secrets", terminal))
else:
    print("no moks-terminal-secrets.yaml: run scripts/create-terminal-secrets.sh before the terminal starts", file=sys.stderr)
print(yaml.safe_dump_all(out))
PY
) "$REPO" | k create -f -

k get secret moks-secret-sync -o name > /dev/null 2>&1 \
  || NO_AWS_KEYS=1 "$REPO/scripts/create-secret-sync-secret.sh" "$CONTEXT"   # rerun without NO_AWS_KEYS for AWS keys
k get secrets
