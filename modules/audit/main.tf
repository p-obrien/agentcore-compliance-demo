# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------
# Three layers, all carrying enough to reconstruct one interaction:
#   1. DynamoDB audit table (append-only via IAM: writers get PutItem only)
#   2. Bedrock model invocation logging -> S3 (prompt/response/guardrail trace)
#   3. CloudTrail data events on the audit + records tables and the tool Lambda
#
# tenant_id is a first-class attribute on every audit item.

terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.70" }
  }
}

data "aws_caller_identity" "current" {}

# --- 1. append-only audit table --------------------------------------------
resource "aws_dynamodb_table" "audit" {
  name         = "${var.name_prefix}-audit"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "interaction_id"
  range_key    = "ts"

  attribute {
    name = "interaction_id"
    type = "S"
  }
  attribute {
    name = "ts"
    type = "N"
  }
  attribute {
    name = "tenant_id"
    type = "S"
  }

  # Query all interactions for a tenant, newest first (for the trace view).
  global_secondary_index {
    name            = "by-tenant"
    hash_key        = "tenant_id"
    range_key       = "ts"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = false # short-lived lab
  }

  tags = var.tags
}

# --- 1b. trace read API: project all events for one Interaction_ID ----------
data "archive_file" "trace_read" {
  type        = "zip"
  source_dir  = "${path.module}/src/trace_read"
  output_path = "${path.module}/build/trace_read.zip"
  excludes    = ["__pycache__"] # never ship stale bytecode that can shadow the .py
}

resource "aws_iam_role" "trace_read" {
  name                 = "${var.name_prefix}-trace-read-role"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

# Read-only: Query the audit table by interaction_id. No write, no delete.
resource "aws_iam_role_policy" "trace_read" {
  name = "${var.name_prefix}-trace-read-policy"
  role = aws_iam_role.trace_read.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:Query"]
        Resource = aws_dynamodb_table.audit.arn
      }
    ]
  })
}

resource "aws_lambda_function" "trace_read" {
  function_name    = "${var.name_prefix}-trace-read"
  role             = aws_iam_role.trace_read.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.trace_read.output_path
  source_code_hash = data.archive_file.trace_read.output_base64sha256
  timeout          = 15

  environment {
    variables = {
      AUDIT_TABLE = aws_dynamodb_table.audit.name
    }
  }
  tags = var.tags
}

# --- 2. Bedrock invocation logging to S3 -----------------------------------
resource "aws_s3_bucket" "bedrock_logs" {
  bucket        = "${var.name_prefix}-bedrock-logs-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
  tags          = var.tags
}

resource "aws_s3_bucket_public_access_block" "bedrock_logs" {
  bucket                  = aws_s3_bucket.bedrock_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Allow the Bedrock logging service to write to the bucket.
resource "aws_s3_bucket_policy" "bedrock_logs" {
  bucket = aws_s3_bucket.bedrock_logs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock.amazonaws.com" }
      Action    = "s3:PutObject"
      Resource  = "${aws_s3_bucket.bedrock_logs.arn}/*"
      Condition = {
        StringEquals = { "aws:SourceAccount" = var.account_id }
      }
    }]
  })
}

resource "aws_bedrock_model_invocation_logging_configuration" "this" {
  logging_config {
    embedding_data_delivery_enabled = false
    image_data_delivery_enabled     = false
    text_data_delivery_enabled      = true

    s3_config {
      bucket_name = aws_s3_bucket.bedrock_logs.id
      key_prefix  = "invocations"
    }
  }

  depends_on = [aws_s3_bucket_policy.bedrock_logs]
}

# --- 3. CloudTrail data events ---------------------------------------------
resource "aws_s3_bucket" "trail" {
  bucket        = "${var.name_prefix}-trail-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
  tags          = var.tags
}

resource "aws_s3_bucket_public_access_block" "trail" {
  bucket                  = aws_s3_bucket.trail.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "trail" {
  bucket = aws_s3_bucket.trail.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AWSCloudTrailAclCheck"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:GetBucketAcl"
        Resource  = aws_s3_bucket.trail.arn
      },
      {
        Sid       = "AWSCloudTrailWrite"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.trail.arn}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"
        Condition = {
          StringEquals = { "s3:x-amz-acl" = "bucket-owner-full-control" }
        }
      }
    ]
  })
}

resource "aws_cloudtrail" "this" {
  name                          = "${var.name_prefix}-trail"
  s3_bucket_name                = aws_s3_bucket.trail.id
  include_global_service_events = false
  is_multi_region_trail         = false
  enable_log_file_validation    = true

  # Data events on every DynamoDB table, which covers the staging (pending
  # approvals), records (mock system-of-record), and audit tables, so every
  # read/write to produced data and audit evidence is trailed.
  advanced_event_selector {
    name = "DynamoDB data events"
    field_selector {
      field  = "eventCategory"
      equals = ["Data"]
    }
    field_selector {
      field  = "resources.type"
      equals = ["AWS::DynamoDB::Table"]
    }
  }

  # Data events on Lambda invocations (the retrieval tool).
  advanced_event_selector {
    name = "Lambda data events"
    field_selector {
      field  = "eventCategory"
      equals = ["Data"]
    }
    field_selector {
      field  = "resources.type"
      equals = ["AWS::Lambda::Function"]
    }
  }

  depends_on = [aws_s3_bucket_policy.trail]
  tags       = var.tags
}
