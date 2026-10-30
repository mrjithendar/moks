#!/usr/bin/env bash
# Run from the repository root (moks/). -chdir is a global option: it goes BEFORE the subcommand,
# and -var-file / -out are then relative to deploy/terraform.
set -euo pipefail
cd "$(dirname "$0")"
TF="terraform -chdir=deploy/terraform"

$TF init -input=false
$TF plan -var-file=tfvars/mk4.tfvars -out=moks.tfplan
read -r -p "Apply this plan? [y/N] " ok
[[ "$ok" == "y" ]] || { echo "Not applied."; exit 0; }
$TF apply moks.tfplan

gh variable set AWS_ROLE_ARN --repo mrjithendar/moks --body "$($TF output -raw ci_role_arn)"

# After scripts/create-secrets.sh has created moks/moks-secrets:
#   scripts/create-runtime-key.sh mk4
