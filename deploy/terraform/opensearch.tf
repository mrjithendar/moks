# Optional managed search for MOKS knowledge. One node to start with: the index is rebuilt from
# PostgreSQL by a reindex, so it holds nothing that cannot be recreated. mk4 is outside AWS, so the
# endpoint is public; only the runtime user may call it, with signed requests over TLS.
locals {
  opensearch_enabled = var.opensearch_domain_name != "" && var.runtime_user_enabled
}

resource "aws_opensearch_domain" "moks" {
  count          = local.opensearch_enabled ? 1 : 0
  domain_name    = var.opensearch_domain_name
  engine_version = var.opensearch_engine_version

  cluster_config {
    instance_type  = var.opensearch_instance_type
    instance_count = 1
  }

  ebs_options {
    ebs_enabled = true
    volume_type = "gp3"
    volume_size = var.opensearch_storage_gb
  }

  encrypt_at_rest {
    enabled = true
  }

  node_to_node_encryption {
    enabled = true
  }

  domain_endpoint_options {
    enforce_https       = true
    tls_security_policy = "Policy-Min-TLS-1-2-2019-07"
  }

  access_policies = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = aws_iam_user.runtime[0].arn }
      Action    = ["es:ESHttpGet", "es:ESHttpHead", "es:ESHttpPost", "es:ESHttpPut", "es:ESHttpDelete"]
      Resource  = "arn:${local.partition}:es:${var.aws_region}:${local.account_id}:domain/${var.opensearch_domain_name}/*"
    }]
  })
}
