# Immutable tags: CI pushes moks:<commit-sha> once and Argo CD deploys a pinned digest.
resource "aws_ecr_repository" "moks" {
  name                 = var.repository_name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false # never lose the images a rollback needs

  encryption_configuration {
    encryption_type = "AES256"
  }

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "moks" {
  repository = aws_ecr_repository.moks.name
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Remove untagged layers left by failed or superseded pushes"
        selection    = { tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = 1 }
        action       = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the newest ${var.image_retention_count} commit images"
        selection    = { tagStatus = "tagged", tagPatternList = ["*"], countType = "imageCountMoreThan", countNumber = var.image_retention_count }
        action       = { type = "expire" }
      },
    ]
  })
}

resource "aws_ecr_repository_policy" "pull" {
  count      = length(var.ecr_pull_principal_arns) > 0 ? 1 : 0
  repository = aws_ecr_repository.moks.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowListedPull"
      Effect    = "Allow"
      Principal = { AWS = var.ecr_pull_principal_arns }
      Action    = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
    }]
  })
}
