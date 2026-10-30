#!/usr/bin/env bash
# Create (or rotate) the access key of the Terraform-managed IAM user moks-mk4-runtime and store
# it in the moks-secrets Secret as AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (read by the AWS
# SDK inside MOKS for Bedrock and, later, CloudWatch).
#
#   scripts/create-runtime-key.sh [kube-context]          # first key
#   ROTATE=1 scripts/create-runtime-key.sh [kube-context] # new key, then delete the old one(s)
#
# The secret key goes from AWS to Kubernetes through a mode-600 temp file; it is never printed,
# never passed on a command line and never stored in Terraform state.
set -euo pipefail
CONTEXT="${1:-mk4}"
USER_NAME="${IAM_USER:-moks-mk4-runtime}"
k() { kubectl --context "$CONTEXT" -n moks "$@"; }

aws iam get-user --user-name "$USER_NAME" >/dev/null || { echo "IAM user $USER_NAME not found; run terraform apply in deploy/terraform first." >&2; exit 1; }
k get secret moks-secrets >/dev/null || { echo "Secret moks/moks-secrets missing; run scripts/create-secrets.sh first." >&2; exit 1; }

OLD_KEYS="$(aws iam list-access-keys --user-name "$USER_NAME" --query 'AccessKeyMetadata[].AccessKeyId' --output text)"
if [[ -n "$OLD_KEYS" && "${ROTATE:-0}" != "1" ]]; then
  echo "$USER_NAME already has key(s) $OLD_KEYS. Rerun with ROTATE=1 to replace them." >&2
  exit 1
fi

umask 077
PATCH="$(mktemp)"
trap 'rm -f "$PATCH"' EXIT
aws iam create-access-key --user-name "$USER_NAME" --output json \
  | python3 -c 'import json,sys; k=json.load(sys.stdin)["AccessKey"]; json.dump({"stringData":{"AWS_ACCESS_KEY_ID":k["AccessKeyId"],"AWS_SECRET_ACCESS_KEY":k["SecretAccessKey"]}}, sys.stdout)' > "$PATCH"
k patch secret moks-secrets --type merge --patch-file "$PATCH" >/dev/null
echo "→ Stored a new key for $USER_NAME in moks/moks-secrets"

k rollout restart deploy/moks-api deploy/moks-worker 2>/dev/null && echo "→ Restarted MOKS to load it" || echo "→ MOKS not deployed yet; it will read the key at first start"

for old in $OLD_KEYS; do
  aws iam delete-access-key --user-name "$USER_NAME" --access-key-id "$old"
  echo "→ Deleted old key $old"
done
