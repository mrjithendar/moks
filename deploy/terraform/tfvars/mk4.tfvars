# MOKS on mk4 (Hetzner Minikube), account 881026659929. No secrets in this file.
aws_region            = "us-east-1"
repository_name       = "moks"
image_retention_count = 30

github_repository = "mrjithendar/moks"
github_push_refs  = ["refs/heads/master"]
# The repository uses GitHub's immutable OIDC subject (owner and repo IDs survive renames and cannot
# be claimed by a new repository that reuses the name). Source: gh api repos/mrjithendar/moks/actions/oidc/customization/sub
github_oidc_subject_prefix  = "repo:mrjithendar@20453233/moks@1439332847"
create_github_oidc_provider = true # none existed in the account on 2026-10-30

# Principal behind mk4's ecr-refresh keys (roboshop-secrets), only if its own policy does not
# already allow pulls on every repository. See README.md "Image pulls on mk4".
ecr_pull_principal_arns = []

runtime_user_enabled         = true
bedrock_enabled              = true
bedrock_inference_profile_id = "us.amazon.nova-2-lite-v1:0" # same profile RoboShop's Moks uses
bedrock_model_id             = "amazon.nova-2-lite-v1:0"
cloudwatch_log_group_arns    = [] # Phase 2
