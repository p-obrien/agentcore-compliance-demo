output "state_bucket_name" {
  description = "Put this in ../backend.tf as `bucket`."
  value       = aws_s3_bucket.state.id
}

output "lock_table_name" {
  description = "Put this in ../backend.tf as `dynamodb_table`."
  value       = aws_dynamodb_table.lock.name
}

output "backend_config_snippet" {
  description = "Copy-paste into ../backend.tf, then `tofu init` the root."
  value       = <<-EOT
    terraform {
      backend "s3" {
        bucket         = "${aws_s3_bucket.state.id}"
        key            = "agentcore-compliance-demo/terraform.tfstate"
        region         = "${var.aws_region}"
        dynamodb_table = "${aws_dynamodb_table.lock.name}"
        encrypt        = true
      }
    }
  EOT
}
