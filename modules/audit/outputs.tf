output "audit_table_name" {
  value = aws_dynamodb_table.audit.name
}

output "audit_table_arn" {
  value = aws_dynamodb_table.audit.arn
}

output "audit_table_gsi_name" {
  value = "by-tenant"
}

output "trace_read_function_name" {
  value = aws_lambda_function.trace_read.function_name
}

output "trace_read_function_arn" {
  value = aws_lambda_function.trace_read.arn
}

output "bedrock_logs_bucket" {
  value = aws_s3_bucket.bedrock_logs.id
}

output "trail_name" {
  value = aws_cloudtrail.this.name
}
