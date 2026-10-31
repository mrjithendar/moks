# Identity for MOKS pods on mk4 (outside AWS, so no IRSA). Terraform creates the user and its
# least-privilege policies but NOT an access key, so no secret ever lands in Terraform state.
# scripts/create-runtime-key.sh creates the key and writes it straight into the moks-secrets Secret.
# On EKS use IRSA/Pod Identity with the same policy documents instead (deploy/iam/).
locals {
  bedrock_profile_arn = "arn:${local.partition}:bedrock:${var.aws_region}:${local.account_id}:inference-profile/${var.bedrock_inference_profile_id}"
}

resource "aws_iam_user" "runtime" {
  count = var.runtime_user_enabled ? 1 : 0
  name  = "moks-mk4-runtime"
  path  = "/moks/"
}

resource "aws_iam_user_policy" "bedrock" {
  count = var.runtime_user_enabled && var.bedrock_enabled ? 1 : 0
  name  = "invoke-selected-bedrock-profile"
  user  = aws_iam_user.runtime[0].name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # bedrock:InvokeModel also authorizes the Converse API that MOKS uses.
        Sid      = "InvokeSelectedProfile"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = local.bedrock_profile_arn
      },
      {
        Sid      = "InvokeProfileDestinationModels"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = "arn:${local.partition}:bedrock:*::foundation-model/${var.bedrock_model_id}"
        # Only through the profile above; direct model calls are refused.
        Condition = { StringEquals = { "bedrock:InferenceProfileArn" = local.bedrock_profile_arn } }
      },
    ]
  })
}

# Current Claude models use the Messages API on bedrock-mantle, which has its own IAM namespace.
resource "aws_iam_user_policy" "bedrock_claude" {
  count = var.runtime_user_enabled && var.bedrock_claude_enabled ? 1 : 0
  name  = "invoke-claude-on-bedrock"
  user  = aws_iam_user.runtime[0].name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "CreateClaudeInference"
      Effect   = "Allow"
      Action   = ["bedrock-mantle:CreateInference"]
      Resource = "arn:${local.partition}:bedrock-mantle:${var.aws_region}:${local.account_id}:project/*"
    }]
  })
}

resource "aws_iam_user_policy" "cloudwatch" {
  count = var.runtime_user_enabled && length(var.cloudwatch_log_group_arns) > 0 ? 1 : 0
  name  = "read-allowed-log-groups"
  user  = aws_iam_user.runtime[0].name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "QueryAllowedLogGroups"
        Effect   = "Allow"
        Action   = ["logs:StartQuery", "logs:GetLogEvents", "logs:FilterLogEvents"]
        Resource = var.cloudwatch_log_group_arns
      },
      {
        # These APIs do not support resource-level restriction.
        Sid      = "QueryLifecycleAndDiscovery"
        Effect   = "Allow"
        Action   = ["logs:GetQueryResults", "logs:StopQuery", "logs:DescribeLogGroups", "cloudwatch:GetMetricData", "cloudwatch:ListMetrics", "cloudwatch:DescribeAlarms"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_iam_user_policy" "finops" {
  count = var.runtime_user_enabled && var.finops_enabled ? 1 : 0
  name  = "read-cost-and-budgets"
  user  = aws_iam_user.runtime[0].name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Read-only; these APIs have no resource-level permissions.
        Sid      = "CostExplorerRead"
        Effect   = "Allow"
        Action   = ["ce:GetCostAndUsage", "ce:GetCostForecast", "ce:GetAnomalies", "ce:GetAnomalyMonitors"]
        Resource = "*"
      },
      {
        Sid      = "BudgetsRead"
        Effect   = "Allow"
        Action   = ["budgets:ViewBudget"]
        Resource = "arn:${local.partition}:budgets::${local.account_id}:budget/*"
      },
      {
        Sid      = "WhoAmI"
        Effect   = "Allow"
        Action   = ["sts:GetCallerIdentity"]
        Resource = "*"
      },
    ]
  })
}
