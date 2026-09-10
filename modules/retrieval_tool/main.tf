# ---------------------------------------------------------------------------
# Retrieval MCP tool: HMAC session verification + tenant-role SigV4 query
# against Amazon OpenSearch Service managed domains (service "es").
#
# The Lambda has no direct OpenSearch permission. After verifying the HMAC
# capability, the authenticated tenant selects one managed-domain read role
# (shared for A/B, dedicated for C), which it assumes before signing the
# request. VPC placement and es:ESHttp* scoping are finalized in the IAM task.
# ---------------------------------------------------------------------------

terraform {
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.70" }
    archive = { source = "hashicorp/archive", version = "~> 2.6" }
  }
}

locals {
  function_name = "${var.name_prefix}-retrieval-tool"
}

data "archive_file" "tool" {
  type        = "zip"
  source_dir  = "${path.module}/src"
  output_path = "${path.module}/build/retrieval_tool.zip"
}

resource "aws_iam_role" "tool" {
  name                 = "${local.function_name}-role"
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

resource "aws_iam_role_policy" "tool" {
  name = "${local.function_name}-policy"
  role = aws_iam_role.tool.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect = "Allow"
        Action = [
          "ec2:CreateNetworkInterface",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DeleteNetworkInterface",
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = var.session_context_secret_arn
      },
      {
        # The authenticated tenant selects one of these managed-domain read
        # roles only after the HMAC capability has been verified. The Lambda
        # has no direct es:ESHttp* permission and cannot reach either domain as
        # itself.
        Effect   = "Allow"
        Action   = ["sts:AssumeRole"]
        Resource = distinct([for target in values(var.managed_domain_targets) : target.role_arn])
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = var.audit_table_arn
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "tool" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 7
  tags              = var.tags
}

resource "aws_lambda_function" "tool" {
  function_name    = local.function_name
  role             = aws_iam_role.tool.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.tool.output_path
  source_code_hash = data.archive_file.tool.output_base64sha256
  timeout          = 15
  memory_size      = 256

  vpc_config {
    subnet_ids         = var.vpc_subnet_ids
    security_group_ids = [var.retrieval_security_group_id]
  }

  environment {
    variables = {
      ES_TARGETS_JSON            = jsonencode(var.managed_domain_targets)
      SESSION_CONTEXT_SECRET_ARN = var.session_context_secret_arn
      AUDIT_TABLE                = var.audit_table_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.tool]
  tags       = var.tags
}
