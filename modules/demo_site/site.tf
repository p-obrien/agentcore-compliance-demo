# ---------------------------------------------------------------------------
# Static demo site: private S3 + CloudFront (OAC), same shape as approval_site.
# ---------------------------------------------------------------------------
# The bucket is private. Only this CloudFront distribution reads it. The page is
# rendered with the API base URL, Cognito Hosted UI domain, agent client id, the
# redirect URI, and the approval page URL baked in at apply time.

locals {
  site_render = {
    api_base_url             = aws_apigatewayv2_stage.this.invoke_url
    cognito_hosted_ui_domain = var.cognito_hosted_ui_domain
    cognito_client_id        = var.cognito_agent_client_id
    redirect_uri             = "https://${aws_cloudfront_distribution.site.domain_name}"
    approval_page_url        = var.approval_page_url
  }
}

resource "aws_s3_bucket" "site" {
  bucket        = "${var.name_prefix}-demo-site-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
  tags          = var.tags
}

resource "aws_s3_bucket_public_access_block" "site" {
  bucket                  = aws_s3_bucket.site.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "index" {
  bucket       = aws_s3_bucket.site.id
  key          = "index.html"
  content_type = "text/html"
  content      = templatefile("${path.module}/site/index.html.tftpl", local.site_render)
  etag         = md5(templatefile("${path.module}/site/index.html.tftpl", local.site_render))
}

resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "${var.name_prefix}-demo-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  default_root_object = "index.html"
  comment             = "${var.name_prefix} demo page"
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
