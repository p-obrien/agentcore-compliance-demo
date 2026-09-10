# ---------------------------------------------------------------------------
# Approval web page: API Gateway (HTTP API) + Lambda + S3 + CloudFront (OAC)
# ---------------------------------------------------------------------------
# Authenticated approval web page: API Gateway JWT authorizer + Lambda + S3 +
# CloudFront. Static assets remain public; approval data and decisions require a
# Cognito access token in the demo-approvers group.

terraform {
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.70" }
    archive = { source = "hashicorp/archive", version = "~> 2.6" }
  }
}

# --- API Lambda -------------------------------------------------------------
data "archive_file" "api" {
  type        = "zip"
  source_dir  = "${path.module}/src/api"
  output_path = "${path.module}/build/api.zip"
}

resource "aws_iam_role" "api" {
  name                 = "${var.name_prefix}-approval-api-role"
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
  name = "${var.name_prefix}-approval-api-policy"
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
        Effect   = "Allow"
        Action   = ["dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:PutItem"]
        Resource = [var.pending_table_arn, "${var.pending_table_arn}/index/*", var.audit_table_arn]
      },
      {
        # Resume the suspended state machine.
        Effect   = "Allow"
        Action   = ["states:SendTaskSuccess", "states:SendTaskFailure"]
        Resource = "*"
      }
    ]
  })
}

resource "aws_lambda_function" "api" {
  function_name    = "${var.name_prefix}-approval-api"
  role             = aws_iam_role.api.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256
  timeout          = 15

  environment {
    variables = {
      PENDING_TABLE   = var.pending_table_name
      AUDIT_TABLE     = var.audit_table_name
      APPROVAL_ORIGIN = "https://${aws_cloudfront_distribution.site.domain_name}"
    }
  }
  tags = var.tags
}

# --- HTTP API ---------------------------------------------------------------
resource "aws_apigatewayv2_api" "this" {
  name          = "${var.name_prefix}-approval-api"
  protocol_type = "HTTP"
  cors_configuration {
    allow_origins = ["https://${aws_cloudfront_distribution.site.domain_name}"]
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["authorization", "content-type"]
  }
  tags = var.tags
}

resource "aws_apigatewayv2_integration" "this" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_authorizer" "approval" {
  api_id           = aws_apigatewayv2_api.this.id
  name             = "cognito-approvers"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    audience = [var.cognito_approval_client_id]
    issuer   = var.cognito_issuer
  }
}

resource "aws_apigatewayv2_route" "pending" {
  api_id             = aws_apigatewayv2_api.this.id
  route_key          = "GET /pending"
  target             = "integrations/${aws_apigatewayv2_integration.this.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.approval.id
}

resource "aws_apigatewayv2_route" "decide" {
  api_id             = aws_apigatewayv2_api.this.id
  route_key          = "POST /decide"
  target             = "integrations/${aws_apigatewayv2_integration.this.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.approval.id
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

# --- Static site: S3 + CloudFront (OAC) ------------------------------------
resource "aws_s3_bucket" "site" {
  bucket        = "${var.name_prefix}-approval-site-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
  tags          = var.tags
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket_public_access_block" "site" {
  bucket                  = aws_s3_bucket.site.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Render the page with the API base URL baked in, then upload.
resource "aws_s3_object" "index" {
  bucket       = aws_s3_bucket.site.id
  key          = "index.html"
  content_type = "text/html"
  content = templatefile("${path.module}/site/index.html.tftpl", {
    api_base_url             = aws_apigatewayv2_stage.this.invoke_url
    cognito_hosted_ui_domain = var.cognito_hosted_ui_domain
    cognito_client_id        = var.cognito_approval_client_id
    redirect_uri             = "https://${aws_cloudfront_distribution.site.domain_name}"
  })
  etag = md5(templatefile("${path.module}/site/index.html.tftpl", {
    api_base_url             = aws_apigatewayv2_stage.this.invoke_url
    cognito_hosted_ui_domain = var.cognito_hosted_ui_domain
    cognito_client_id        = var.cognito_approval_client_id
    redirect_uri             = "https://${aws_cloudfront_distribution.site.domain_name}"
  }))
}

resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "${var.name_prefix}-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  default_root_object = "index.html"
  comment             = "${var.name_prefix} approval page"
  price_class         = "PriceClass_100"

  origin {
    domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
    origin_id                = "s3-site"
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  default_cache_behavior {
    target_origin_id       = "s3-site"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    # Managed-CachingDisabled so page edits show immediately during rehearsal.
    cache_policy_id = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = var.tags
}

# Let only this CloudFront distribution read the bucket.
resource "aws_s3_bucket_policy" "site" {
  bucket = aws_s3_bucket.site.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.site.arn}/*"
      Condition = {
        StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.site.arn }
      }
    }]
  })
}
