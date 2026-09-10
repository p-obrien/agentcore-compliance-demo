output "cloudfront_url" {
  description = "Open this in a browser to approve/reject."
  value       = "https://${aws_cloudfront_distribution.site.domain_name}"
}

output "api_base_url" {
  description = "Base URL of the approval API."
  value       = aws_apigatewayv2_stage.this.invoke_url
}

output "site_bucket" {
  value = aws_s3_bucket.site.id
}
