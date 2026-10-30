variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "repository_name" {
  description = "ECR repository for the MOKS image (API, worker and migrations share it)."
  type        = string
  default     = "moks"
}

variable "image_retention_count" {
  description = "Commit-tagged images to keep. Older ones expire; keep enough to roll back."
  type        = number
  default     = 30
  validation {
    condition     = var.image_retention_count >= 5 && var.image_retention_count <= 1000
    error_message = "Keep between 5 and 1000 images."
  }
}

variable "ecr_pull_principal_arns" {
  description = "Extra IAM user/role ARNs allowed to pull (e.g. the principal behind mk4's ecr-refresh keys). Not needed if their own policy already allows ecr pulls on this repository."
  type        = list(string)
  default     = []
  validation {
    condition     = alltrue([for arn in var.ecr_pull_principal_arns : can(regex("^arn:aws:iam::[0-9]{12}:(user|role)/[A-Za-z0-9+=,.@_/-]+$", arn))])
    error_message = "Use exact IAM user or role ARNs (no wildcards, root or STS session ARNs)."
  }
}

# ---- GitHub Actions (CI image push) -------------------------------------------------------------

variable "github_repository" {
  description = "owner/name of the repository whose CI may push images."
  type        = string
  default     = "mrjithendar/moks"
}

variable "github_oidc_subject_prefix" {
  description = <<-EOT
    Start of the GitHub OIDC `sub` claim for this repository, without ":ref:...".
    Repositories with immutable subjects (GitHub setting use_immutable_subject, the default here)
    send "repo:<owner>@<owner-id>/<repo>@<repo-id>"; see
    `gh api repos/<owner>/<repo>/actions/oidc/customization/sub` (field sub_claim_prefix).
    Empty means the classic "repo:<owner>/<repo>".
  EOT
  type        = string
  default     = ""
  validation {
    condition     = var.github_oidc_subject_prefix == "" || can(regex("^repo:[A-Za-z0-9-]+(@[0-9]+)?/[A-Za-z0-9_.-]+(@[0-9]+)?$", var.github_oidc_subject_prefix))
    error_message = "Use repo:<owner>[@<id>]/<repo>[@<id>] as shown by sub_claim_prefix."
  }
}

variable "github_push_refs" {
  description = "Git refs allowed to assume the CI role (the release job runs only on the default branch)."
  type        = list(string)
  default     = ["refs/heads/master"]
}

variable "create_github_oidc_provider" {
  description = "Create the account-wide token.actions.githubusercontent.com OIDC provider. Set false if another stack already manages it."
  type        = bool
  default     = true
}

# ---- MOKS runtime on mk4 (Bedrock, Phase 2 CloudWatch) -----------------------------------------

variable "runtime_user_enabled" {
  description = "Create the IAM user whose access key MOKS pods on mk4 use. No key is created by Terraform (see scripts/create-runtime-key.sh)."
  type        = bool
  default     = true
}

variable "bedrock_enabled" {
  type    = bool
  default = false
}

variable "bedrock_inference_profile_id" {
  description = "Exact US inference profile MOKS may call. Set the same ID as the model in Administration → LLM providers."
  type        = string
  default     = "us.amazon.nova-2-lite-v1:0"
  validation {
    condition     = can(regex("^(us|global)\\.[a-zA-Z0-9.:-]+$", var.bedrock_inference_profile_id))
    error_message = "Use an exact inference profile ID."
  }
}

variable "bedrock_model_id" {
  description = "Foundation model behind the inference profile."
  type        = string
  default     = "amazon.nova-2-lite-v1:0"
  validation {
    condition     = can(regex("^[a-zA-Z0-9.:-]+$", var.bedrock_model_id))
    error_message = "Use an exact model ID without wildcards."
  }
}

variable "cloudwatch_log_group_arns" {
  description = "Phase 2: log group ARNs (wildcards allowed at the end) MOKS may query with Logs Insights. Empty grants nothing."
  type        = list(string)
  default     = []
}

variable "tags" {
  type    = map(string)
  default = {}
}
