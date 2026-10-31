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

output "terminal_role_arn" {
  description = "roleArn for a terminal profile in the Helm values. Null when terminals are disabled."
  value       = try(aws_iam_role.terminal[0].arn, null)
}

output "terminal_gateway_user_name" {
  description = "IAM user for the terminal gateway on mk4. Create its key by hand into moks-terminal-secrets. Null when disabled."
  value       = try(aws_iam_user.terminal_gateway[0].name, null)
}

output "opensearch_url_for_moks" {
  description = "Set as rag.opensearch.url in the Helm values, with rag.backend opensearch. Null when no domain is configured."
  value       = local.opensearch_enabled ? "https://${aws_opensearch_domain.moks[0].endpoint}" : null
}
