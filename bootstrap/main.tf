# ---------------------------------------------------------------------------
# State backend bootstrap (run ONCE, before the main lab)
# ---------------------------------------------------------------------------
# Chicken-and-egg problem: a remote S3 backend cannot create the bucket it
# Local state is intentionally not committed. Migrate it to the encrypted
# backend created here before sharing this lab.
#
# Usage:
#   cd bootstrap
#   export AWS_PROFILE=<your-profile>
#   tofu init && tofu apply
#   # note the outputs, then uncomment ../backend.tf and `tofu init` the root.

terraform {
  required_version = ">= 1.8.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
  # Local state on purpose. Do not add a backend block here.
}

provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile
  default_tags {
    tags = var.tags
  }
}

# Random suffix keeps the bucket name globally unique across re-creates.
resource "random_id" "suffix" {
  byte_length = 4
}

locals {
  bucket_name = "${var.name_prefix}-tfstate-${random_id.suffix.hex}"
  table_name  = "${var.name_prefix}-tflock"
}

resource "aws_s3_bucket" "state" {
  bucket        = local.bucket_name
  force_destroy = true # throwaway lab: allow destroy even with state versions present

  tags = {
    Name = local.bucket_name
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "lock" {
  name         = local.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  tags = {
    Name = local.table_name
  }
}
