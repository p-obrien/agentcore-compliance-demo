# ---------------------------------------------------------------------------
# Demo web interface: backend API (HTTP API + Lambda) for the training session.
# ---------------------------------------------------------------------------
# A signed-in assessor's browser cannot call InvokeAgentRuntime (SigV4, no CORS,
# no AWS credentials), so this Lambda relays the call. It forwards the caller's
# Cognito access and ID tokens unchanged; the runtime and retrieval Lambda still
# derive tenant from the verified claims. The Lambda also serves a tenant-scoped,
# read-only projection of the audit table for the live audit panel.
#
# The static page (S3 + CloudFront) lives in site.tf.

terraform {
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.70" }
    archive = { source = "hashicorp/archive", version = "~> 2.6" }
  }
}

data "aws_caller_identity" "current" {}

locals {
  # The demo API invokes the assessment runtime and any of its endpoints. Until
  # an agent image is applied the runtime ARN is empty; fall back to a
  # non-matching ARN so the IAM policy stays valid and grants nothing.
  runtime_invoke_resources = var.assessment_runtime_arn != "" ? [
    var.assessment_runtime_arn,
    "${var.assessment_runtime_arn}/runtime-endpoint/*",
  ] : ["arn:aws:bedrock-agentcore:*:${data.aws_caller_identity.current.account_id}:runtime/__none__"]
}

# --- API Lambda -------------------------------------------------------------
data "archive_file" "api" {
  type        = "zip"
  source_dir  = "${path.module}/src/api"
  output_path = "${path.module}/build/api.zip"

  # Never ship compiled bytecode. A stale .pyc from a local test run (or one
  # compiled by a different Python than the Lambda runtime) can shadow the .py
  # and run old code, which surfaced as "module 'runtime' has no attribute
  # 'RuntimeInvocationError'" at runtime while the .py source was correct.
  excludes = ["__pycache__"]
}

resource "aws_iam_role" "api" {
  name                 = "${var.name_prefix}-demo-api-role"
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

resource "aws_iam_role_policy" "api" {
  name = "${var.name_prefix}-demo-api-policy"
  role = aws_iam_role.api.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        # Relay assessments to the assessment runtime only. No other runtime,
        # and no tenant scope decided here.
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:InvokeAgentRuntime"]
        Resource = local.runtime_invoke_resources
      },
      {
        # Read-only audit projection: Query the table and its by-tenant GSI.
        # No PutItem, no UpdateItem: the demo API cannot alter the trail.
        Effect   = "Allow"
        Action   = ["dynamodb:Query"]
        Resource = [var.audit_table_arn, "${var.audit_table_arn}/index/*"]
      }
    ]
  })
}

resource "aws_lambda_function" "api" {
  function_name    = "${var.name_prefix}-demo-api"
  role             = aws_iam_role.api.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256
  # An assessment runs the model end to end before returning (buffered), so the
  # relay call needs a generous budget. The API Gateway integration caps at 30s;
  # keep the function under that to return a clean error rather than a 504.
  timeout = 29

  environment {
    variables = {
      ASSESSMENT_RUNTIME_ARN = var.assessment_runtime_arn
      AUDIT_TABLE            = var.audit_table_name
      AUDIT_BY_TENANT_INDEX  = var.audit_by_tenant_index
      DEMO_ORIGIN            = "https://${aws_cloudfront_distribution.site.domain_name}"
    }
  }
  tags = var.tags
}

# --- HTTP API ---------------------------------------------------------------
resource "aws_apigatewayv2_api" "this" {
  name          = "${var.name_prefix}-demo-api"
  protocol_type = "HTTP"
  cors_configuration {
    allow_origins = ["https://${aws_cloudfront_distribution.site.domain_name}"]
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["authorization", "content-type", "x-id-token"]
  }
  tags = var.tags
}

resource "aws_apigatewayv2_integration" "this" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
  # The buffered assessment relay can run for most of the 30s ceiling.
  timeout_milliseconds = 30000
}

resource "aws_apigatewayv2_authorizer" "agent" {
  api_id           = aws_apigatewayv2_api.this.id
  name             = "cognito-assessors"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    audience = [var.cognito_agent_client_id]
    issuer   = var.cognito_issuer
  }
}

resource "aws_apigatewayv2_route" "assess" {
  api_id             = aws_apigatewayv2_api.this.id
  route_key          = "POST /assess"
  target             = "integrations/${aws_apigatewayv2_integration.this.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.agent.id
}

resource "aws_apigatewayv2_route" "audit" {
  api_id             = aws_apigatewayv2_api.this.id
  route_key          = "GET /audit"
  target             = "integrations/${aws_apigatewayv2_integration.this.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.agent.id
}

resource "aws_apigatewayv2_route" "audit_recent" {
  api_id             = aws_apigatewayv2_api.this.id
  route_key          = "GET /audit/recent"
  target             = "integrations/${aws_apigatewayv2_integration.this.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.agent.id
}

resource "aws_apigatewayv2_stage" "this" {
  api_id      = aws_apigatewayv2_api.this.id
  name        = "$default"
  auto_deploy = true
  tags        = var.tags
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}
