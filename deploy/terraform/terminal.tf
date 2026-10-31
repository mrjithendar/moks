# What a terminal session can do in AWS is decided here. Shell wrappers cannot make a session
# read-only: this role does. The gateway's base identity can do one thing, which is assume it.
locals {
  terminal_gateway_arns = concat(aws_iam_user.terminal_gateway[*].arn, var.terminal_gateway_role_arns)
}

# mk4 is outside AWS, so the gateway has a user of its own, apart from moks-mk4-runtime. As with that
# user, Terraform creates no key.
resource "aws_iam_user" "terminal_gateway" {
  count = var.terminal_enabled ? 1 : 0
  name  = "moks-terminal-gateway"
  path  = "/moks/"
}

resource "aws_iam_user_policy" "terminal_gateway" {
  count = var.terminal_enabled ? 1 : 0
  name  = "assume-terminal-role"
  user  = aws_iam_user.terminal_gateway[0].name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "AssumeTerminalRole"
      Effect   = "Allow"
      Action   = ["sts:AssumeRole", "sts:SetSourceIdentity"]
      Resource = aws_iam_role.terminal[0].arn
    }]
  })
}

resource "aws_iam_role" "terminal" {
  count                = var.terminal_enabled ? 1 : 0
  name                 = "moks-terminal-readonly"
  path                 = "/moks/"
  description          = "Assumed once per MOKS terminal session. Read-only."
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyTheGatewayAndNamedForASession"
      Effect    = "Allow"
      Principal = { AWS = local.terminal_gateway_arns }
      Action    = ["sts:AssumeRole", "sts:SetSourceIdentity"]
      Condition = {
        StringLike = { "sts:RoleSessionName" = ["moks-term-*", "moks-audit-*"] }
        Null       = { "sts:SourceIdentity" = "false" } # every session names the person who opened it
      }
    }]
  })
}

# ViewOnlyAccess lists and describes. Unlike ReadOnlyAccess it does not read data such as S3 objects.
resource "aws_iam_role_policy_attachment" "terminal_view" {
  count      = var.terminal_enabled ? 1 : 0
  role       = aws_iam_role.terminal[0].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/job-function/ViewOnlyAccess"
}

resource "aws_iam_role_policy" "terminal" {
  count = var.terminal_enabled ? 1 : 0
  name  = "terminal-session"
  role  = aws_iam_role.terminal[0].name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid      = "SessionAudit"
        Effect   = "Allow"
        Action   = ["cloudtrail:LookupEvents"]
        Resource = "*"
      },
      {
        # An explicit deny, so a broad policy attached by mistake later still cannot read these.
        Sid    = "NeverSecrets"
        Effect = "Deny"
        Action = [
          "secretsmanager:GetSecretValue", "ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath",
          "kms:Decrypt", "s3:GetObject", "iam:CreateAccessKey", "sts:AssumeRole",
        ]
        Resource = "*"
      },
      ], length(var.terminal_eks_cluster_arns) > 0 ? [{
        Sid      = "FindClusters"
        Effect   = "Allow"
        Action   = ["eks:DescribeCluster"]
        Resource = var.terminal_eks_cluster_arns
    }] : [])
  })
}
