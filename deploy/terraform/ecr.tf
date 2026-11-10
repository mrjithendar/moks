# CI pushes every image as latest, its only tag, so tags are mutable. Earlier builds stay as untagged images
# (kept by the lifecycle policies below) and a rollback points latest back at one by digest (docs/RELEASES.md).
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

# The terminal's diagnostic image. Sessions run it by digest, so moving latest cannot change what runs, and
# nothing here expires: an old digest must stay pullable until the chart moves on from it.
resource "aws_ecr_repository" "terminal" {
  name                 = "${var.repository_name}-terminal"
  image_tag_mutability = "MUTABLE"
  force_delete         = false

  encryption_configuration {
    encryption_type = "AES256"
  }

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "reranker" {
  name                 = "${var.repository_name}-reranker"
  image_tag_mutability = "MUTABLE" # latest has to move, like the moks repository
  force_delete         = false

  encryption_configuration {
    encryption_type = "AES256"
  }

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "opensearch" {
  name                 = "${var.repository_name}-opensearch"
  image_tag_mutability = "MUTABLE" # latest moves on every build; the chart pins a digest, so that changes nothing that runs
  force_delete         = false

  encryption_configuration {
    encryption_type = "AES256"
  }

  image_scanning_configuration {
    scan_on_push = true
  }
}

# The MOKS image built with the CrewAI extra (Dockerfile --build-arg MOKS_CREWAI=true, docs/crewai-investigation.md).
# Its own repository, so the default moks:latest never carries crewai and its dependencies. mk4 runs it only when
# values-hetzner.yaml points image.repository here.
resource "aws_ecr_repository" "crewai" {
  name                 = "${var.repository_name}-crewai"
  image_tag_mutability = "MUTABLE" # latest has to move, like the moks repository
  force_delete         = false

  encryption_configuration {
    encryption_type = "AES256"
  }

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "crewai" {
  repository = aws_ecr_repository.crewai.name
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
