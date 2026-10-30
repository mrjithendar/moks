# Every image is tagged with its commit and deployed by digest, so a tag can never move.
# The old :latest images stay until they age out, and a rollback pins a digest from Git history.
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
        description  = "Keep the newest ${var.image_retention_count} images for rollback"
        selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = var.image_retention_count }
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
