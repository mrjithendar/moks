# Every image is tagged with its commit, and CI moves latest to the newest build, which mk4 runs.
# latest has to move, so tags are mutable. A rollback points latest back at a commit tag.
resource "aws_ecr_repository" "moks" {
  name                 = var.repository_name
  image_tag_mutability = "MUTABLE"
  force_delete         = false # never lose the images a rollback needs

  encryption_configuration {
    encryption_type = "AES256"
  }

  image_scanning_configuration {
    scan_on_push = true
  }
}

# The terminal's diagnostic image. Sessions run it by digest, so tags cannot change what runs, and
# nothing here expires: an old digest must stay pullable until the chart moves on from it.
resource "aws_ecr_repository" "terminal" {
  name                 = "${var.repository_name}-terminal"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false

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
