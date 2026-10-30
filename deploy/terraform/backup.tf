# Optional off-cluster backup bucket. Versioning means a deleted or overwritten backup stays
# recoverable; the runtime user can write and read but never remove a version. Object Lock can only
# be chosen when the bucket is created. Terraform creates no access key, so nothing secret lands in state.
resource "aws_s3_bucket" "backup" {
  count               = var.backup_bucket_name != "" ? 1 : 0
  bucket              = var.backup_bucket_name
  object_lock_enabled = var.backup_object_lock_days > 0
}

resource "aws_s3_bucket_versioning" "backup" {
  count  = var.backup_bucket_name != "" ? 1 : 0
  bucket = aws_s3_bucket.backup[0].id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_public_access_block" "backup" {
  count                   = var.backup_bucket_name != "" ? 1 : 0
  bucket                  = aws_s3_bucket.backup[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backup" {
  count  = var.backup_bucket_name != "" ? 1 : 0
  bucket = aws_s3_bucket.backup[0].id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_object_lock_configuration" "backup" {
  count  = var.backup_bucket_name != "" && var.backup_object_lock_days > 0 ? 1 : 0
  bucket = aws_s3_bucket.backup[0].id
  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = var.backup_object_lock_days
    }
  }
  depends_on = [aws_s3_bucket_versioning.backup]
}

# Superseded versions are kept for this long, then removed by S3 rather than by MOKS.
resource "aws_s3_bucket_lifecycle_configuration" "backup" {
  count  = var.backup_bucket_name != "" ? 1 : 0
  bucket = aws_s3_bucket.backup[0].id
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = var.backup_noncurrent_days }
  }
  depends_on = [aws_s3_bucket_versioning.backup]
}

resource "aws_iam_user" "backup" {
  count = var.backup_bucket_name != "" ? 1 : 0
  name  = "moks-mk4-backup"
  path  = "/moks/"
}

resource "aws_iam_user_policy" "backup" {
  count = var.backup_bucket_name != "" ? 1 : 0
  name  = "write-moks-backups"
  user  = aws_iam_user.backup[0].name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.backup[0].arn },
      # DeleteObject adds a delete marker for retention. DeleteObjectVersion is deliberately absent.
      { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"], Resource = "${aws_s3_bucket.backup[0].arn}/*" },
    ]
  })
}
