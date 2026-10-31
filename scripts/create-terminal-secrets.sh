#!/usr/bin/env bash
# Create (or rotate) everything the terminal gateway holds: an access key of the Terraform-managed
# IAM user moks-terminal-gateway, a password for the moks_terminal database role, and the
# moks-terminal-secrets Secret that carries them (docs/TERMINAL.md).
#
#   scripts/create-terminal-secrets.sh [kube-context]          # first time
#   ROTATE=1 scripts/create-terminal-secrets.sh [kube-context] # new key and password, then delete the old key(s)
#
# Run it before syncing terminal.enabled: true, or the gateway has no Secret to start with.
# Values go through mode-600 temp files; they are never printed or passed on a command line.
set -euo pipefail
CONTEXT="${1:-mk4}"
USER_NAME="${IAM_USER:-moks-terminal-gateway}"
SECRET=moks-terminal-secrets
k() { kubectl --context "$CONTEXT" -n moks "$@"; }

aws iam get-user --user-name "$USER_NAME" >/dev/null || { echo "IAM user $USER_NAME not found; run terraform apply in deploy/terraform with terminal_enabled = true first." >&2; exit 1; }
k get secret moks-secrets >/dev/null || { echo "Secret moks/moks-secrets missing; run scripts/create-secrets.sh first." >&2; exit 1; }

OLD_KEYS="$(aws iam list-access-keys --user-name "$USER_NAME" --query 'AccessKeyMetadata[].AccessKeyId' --output text)"
EXISTS="$(k get secret "$SECRET" --ignore-not-found -o name)"
if [[ ( -n "$OLD_KEYS" || -n "$EXISTS" ) && "${ROTATE:-0}" != "1" ]]; then
  echo "$USER_NAME already has key(s) [$OLD_KEYS] and Secret [$EXISTS]. Rerun with ROTATE=1 to replace them." >&2
  exit 1
fi

umask 077
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
python3 -c 'import secrets; print(secrets.token_hex(24))' > "$WORK/password"

# The key comes first: IAM refuses a third one, and nothing has changed yet if it does.
aws iam create-access-key --user-name "$USER_NAME" --output json > "$WORK/key"
echo "→ Created a new key for $USER_NAME"

# moks-api holds the migration owner's URL while secrets.split is false. The password arrives on stdin.
k exec -i deploy/moks-api -- sh -c 'IFS= read -r MOKS_DB_PASSWORD_TERMINAL && export MOKS_DB_PASSWORD_TERMINAL && exec moks db-grants' < "$WORK/password" >/dev/null
echo "→ Set the moks_terminal password and applied database grants"

k get secret moks-secrets -o json > "$WORK/source"
python3 - "$WORK" "$SECRET" > "$WORK/manifest" <<'PY'
import base64, json, sys
work, name = sys.argv[1:3]
key = json.load(open(f"{work}/key"))["AccessKey"]
password = open(f"{work}/password").read().strip()
session = base64.b64decode(json.load(open(f"{work}/source"))["data"]["MOKS_SESSION_SECRET"]).decode()
json.dump({"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "metadata": {"name": name, "namespace": "moks"},
           "stringData": {"MOKS_DATABASE_URL": f"postgresql+asyncpg://moks_terminal:{password}@moks-postgres:5432/moks",
                          "MOKS_SESSION_SECRET": session,
                          "AWS_ACCESS_KEY_ID": key["AccessKeyId"], "AWS_SECRET_ACCESS_KEY": key["SecretAccessKey"]}}, sys.stdout)
PY
if [[ -n "$EXISTS" ]]; then k replace -f "$WORK/manifest" >/dev/null; else k create -f "$WORK/manifest" >/dev/null; fi
echo "→ Stored them in moks/$SECRET"

k rollout restart deploy/moks-terminal 2>/dev/null && echo "→ Restarted the gateway to load them" || echo "→ Gateway not deployed yet; it will read them at first start"

for old in $OLD_KEYS; do
  aws iam delete-access-key --user-name "$USER_NAME" --access-key-id "$old"
  echo "→ Deleted old key $old"
done
