#!/usr/bin/env bash
# Roll back: pin an image digest in values-minikube.yaml instead of :latest. Commit the change afterwards;
# set digest back to "" to follow :latest again.
#   scripts/set-image.sh sha256:<digest>   # e.g. from: aws ecr describe-images --repository-name moks
#   scripts/set-image.sh --latest          # pin whatever :latest is right now
set -euo pipefail
cd "$(dirname "$0")/.."
FILE=deploy/helm/moks/values-minikube.yaml
DIGEST="${1:?usage: set-image.sh sha256:<digest> | --latest}"
if [[ "$DIGEST" == "--latest" ]]; then
  DIGEST="$(aws ecr describe-images --region "${AWS_REGION:-us-east-1}" --repository-name moks --filter tagStatus=TAGGED \
    --query 'sort_by(imageDetails,&imagePushedAt)[-1].imageDigest' --output text)"
fi
[[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "not a digest: $DIGEST" >&2; exit 1; }
sed -i.bak -E "s|^(  digest: )\"[^\"]*\"|\1\"$DIGEST\"|" "$FILE" && rm -f "$FILE.bak"
grep -n "digest:" "$FILE"
