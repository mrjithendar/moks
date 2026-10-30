#!/usr/bin/env bash
# Pin the image the chart deploys. CI runs this in the release pull request; use it by hand to prepare
# a rollback. Commit the result: Git decides what runs.
#   scripts/set-image.sh sha256:<digest>
#   scripts/set-image.sh <commit-sha>       # looks the digest up in ECR
set -euo pipefail
cd "$(dirname "$0")/.."
FILE=deploy/helm/moks/values-minikube.yaml
DIGEST="${1:?usage: set-image.sh sha256:<digest> | <commit-sha>}"
if [[ "$DIGEST" =~ ^[0-9a-f]{7,40}$ ]]; then
  DIGEST="$(aws ecr describe-images --region "${AWS_REGION:-us-east-1}" --repository-name moks --image-ids "imageTag=$DIGEST" \
    --query 'imageDetails[0].imageDigest' --output text)"
fi
[[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "not a digest: $DIGEST" >&2; exit 1; }
sed -i.bak -E "s|^(  digest: )\"[^\"]*\"|\1\"$DIGEST\"|" "$FILE" && rm -f "$FILE.bak"
grep -n "digest:" "$FILE"
