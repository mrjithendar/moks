# Identity for MOKS pods on EKS: Pod Identity gives them short-lived credentials, so no access key is stored. The AWS SDK
# prefers AWS_ACCESS_KEY_ID when it is set, so keep those keys out of moks-secrets on this cluster. The moks ServiceAccount
# runs the API and worker while secrets.split is false.
data "aws_iam_policy_document" "eks_runtime_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "eks_runtime" {
  count              = var.eks_runtime_cluster_name != "" ? 1 : 0
  name               = "moks-eks-runtime"
  assume_role_policy = data.aws_iam_policy_document.eks_runtime_trust.json
}

resource "aws_iam_role_policy" "eks_runtime_bedrock" {
  count = var.eks_runtime_cluster_name != "" ? 1 : 0
  name  = "invoke-bedrock"
  role  = aws_iam_role.eks_runtime[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "CreateClaudeInference"
        Effect   = "Allow"
        Action   = ["bedrock-mantle:CreateInference"]
        Resource = "arn:${local.partition}:bedrock-mantle:${var.aws_region}:${local.account_id}:project/*"
      },
      {
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

resource "aws_eks_pod_identity_association" "eks_runtime" {
  count           = var.eks_runtime_cluster_name != "" ? 1 : 0
  cluster_name    = var.eks_runtime_cluster_name
  namespace       = "moks"
  service_account = "moks"
  role_arn        = aws_iam_role.eks_runtime[0].arn
}
