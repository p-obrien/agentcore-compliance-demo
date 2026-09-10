output "function_name" {
  description = "Name of the one-shot in-VPC Lambda that seeds the managed OpenSearch domains."
  value       = aws_lambda_function.seed_runner.function_name
}

output "function_arn" {
  value = aws_lambda_function.seed_runner.arn
}
