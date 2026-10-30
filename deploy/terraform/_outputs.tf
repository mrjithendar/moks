output "repository_url" {
  description = "Image repository; matches image.repository in deploy/helm/moks/values-minikube.yaml."
  value       = aws_ecr_repository.moks.repository_url
}

output "ci_role_arn" {
  description = "Set as the GitHub repository variable AWS_ROLE_ARN (used by .github/workflows/ci.yml)."
  value       = aws_iam_role.ci.arn
}

output "runtime_user_name" {
  description = "IAM user for MOKS pods on mk4; create its key with scripts/create-runtime-key.sh. Null when disabled."
  value       = try(aws_iam_user.runtime[0].name, null)
}

output "bedrock_model_for_moks" {
  description = "Model ID to enter in Administration → LLM providers (provider bedrock). Null when Bedrock is disabled."
  value       = var.runtime_user_enabled && var.bedrock_enabled ? var.bedrock_inference_profile_id : null
}
