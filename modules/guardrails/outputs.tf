output "guardrail_id" {
  value = aws_bedrock_guardrail.this.guardrail_id
}

output "guardrail_arn" {
  value = aws_bedrock_guardrail.this.guardrail_arn
}

output "guardrail_version" {
  description = "Published guardrail version the agents pin to."
  value       = aws_bedrock_guardrail_version.this.version
}

output "grounding_threshold" {
  value = var.grounding_threshold
}
