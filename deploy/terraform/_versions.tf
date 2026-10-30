terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Same bucket as roboshop-ai/infrastructure, separate state key.
  backend "s3" {
    bucket       = "jithendardharmapuri"
    key          = "terraform/moks.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = merge({
      project    = "moks"
      managed-by = "terraform"
      repository = var.github_repository
    }, var.tags)
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
}
