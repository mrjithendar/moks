# GitHub Actions authenticates with OIDC: no long-lived access key is stored in GitHub.
resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_github_oidc_provider ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

locals {
  github_oidc_provider_arn = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : "arn:${local.partition}:iam::${local.account_id}:oidc-provider/token.actions.githubusercontent.com"
}

resource "aws_iam_role" "ci" {
  name                 = "moks-github-ci"
  description          = "GitHub Actions ${var.github_repository}: push images to ECR ${var.repository_name}"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = local.github_oidc_provider_arn }
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          # Only the listed branches of this repository; pull requests and forks cannot assume it.
          "token.actions.githubusercontent.com:sub" = [for ref in var.github_push_refs : "repo:${var.github_repository}:ref:${ref}"]
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "ci_push" {
  name = "ecr-push-moks"
  role = aws_iam_role.ci.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "Login", Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      {
        Sid    = "PushThisRepositoryOnly"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
          "ecr:DescribeImages", "ecr:DescribeRepositories",
        ]
        Resource = aws_ecr_repository.moks.arn
      },
    ]
  })
}
