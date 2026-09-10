# ---------------------------------------------------------------------------
# One-shot in-VPC OpenSearch seed runner.
# ---------------------------------------------------------------------------
# The managed domains have private VPC endpoints, so seed/load_seed.py cannot
# run from a developer laptop. This Lambda attaches to seed-sg and assumes the
# separate OpenSearch write role before it makes any signed data-plane request.

terraform {
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.70" }
    archive = { source = "hashicorp/archive", version = "~> 2.6" }
  }
}

locals {
  function_name = "${var.name_prefix}-seed-runner"
}

# Package only the runnable loader, fixture data, and Lambda entry point. The
# local virtual environment and test suite must never be uploaded as function
# code.
data "archive_file" "seed_runner" {
  type        = "zip"
  source_dir  = "${path.root}/seed"
  output_path = "${path.module}/build/seed_runner.zip"
  excludes = [
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "tests",
    "load.sh",
    "pyproject.toml",
    "uv.lock",
  ]
}

resource "aws_iam_role" "seed_runner" {
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

resource "aws_iam_role_policy" "seed_runner" {
  name = "${local.function_name}-policy"
  role = aws_iam_role.seed_runner.id
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
        # The runner must assume this role before every OpenSearch request. It
        # deliberately has no direct es:ESHttp* permission.
        Effect   = "Allow"
        Action   = ["sts:AssumeRole"]
        Resource = var.seed_role_arn
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "seed_runner" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 7
  tags              = var.tags
}

resource "aws_lambda_function" "seed_runner" {
  function_name    = local.function_name
  role             = aws_iam_role.seed_runner.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.seed_runner.output_path
  source_code_hash = data.archive_file.seed_runner.output_base64sha256
  timeout          = 300
  memory_size      = 512

  vpc_config {
    subnet_ids         = var.vpc_subnet_ids
    security_group_ids = [var.seed_security_group_id]
  }

  environment {
    variables = {
      SEED_ROLE_ARN     = var.seed_role_arn
      SEED_TARGETS_JSON = jsonencode(var.seed_targets)
    }
  }

  depends_on = [aws_cloudwatch_log_group.seed_runner]
  tags       = var.tags
}
