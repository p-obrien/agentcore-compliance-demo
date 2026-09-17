# Operator-facing values. Sensitive values stay hidden unless explicitly read.

output "approval_page_url" {
  description = "CloudFront approval page. The static page is public, but decisions require Cognito authentication."
  value       = module.approval_site.cloudfront_url
}

output "approval_api_base_url" {
  description = "Cognito JWT-protected approval API base URL."
  value       = module.approval_site.api_base_url
}

output "demo_page_url" {
  description = "CloudFront demo web interface for the guided training walkthrough (assessor login, assessment, live audit panel)."
  value       = module.demo_site.cloudfront_url
}

output "demo_api_base_url" {
  description = "Cognito JWT-protected demo API base URL."
  value       = module.demo_site.api_base_url
}

output "opensearch_managed_domain_endpoints" {
  description = "Shared and dedicated managed-domain HTTPS endpoints. SigV4 and domain access policies still control access."
  value       = module.opensearch.managed_domain_endpoints
}

output "opensearch_seed_targets" {
  description = "Per-tenant endpoint and index map consumed by seed/load.sh."
  value       = module.opensearch.seed_targets
}

output "opensearch_seed_role_arn" {
  description = "Seed-only IAM role that can create indexes and write synthetic documents to both domains."
  value       = module.opensearch.seed_role_arn
}

output "opensearch_domain_admin_role_arn" {
  description = "FGAC master / operator-admin role, held only by the operator."
  value       = module.opensearch.domain_admin_role_arn
}

output "opensearch_shared_retrieval_role_arn" {
  description = "Shared-tier (Agencies A and B) data-plane read role ARN."
  value       = module.opensearch.shared_retrieval_role_arn
}

output "opensearch_dedicated_retrieval_role_arn" {
  description = "Dedicated-tier (Agency C) data-plane read role ARN."
  value       = module.opensearch.dedicated_retrieval_role_arn
}

output "trace_read_function_name" {
  description = "Read-only trace projection Lambda for one Interaction_ID."
  value       = module.audit.trace_read_function_name
}

output "seed_runner_function_name" {
  description = "One-shot private Lambda that loads the synthetic OpenSearch documents during make deploy."
  value       = module.seed_runner.function_name
}

output "audit_table_name" { value = module.audit.audit_table_name }
output "records_table_name" { value = module.approval_flow.records_table_name }
output "state_machine_arn" { value = module.approval_flow.state_machine_arn }
output "retrieval_tool_function_name" { value = module.retrieval_tool.function_name }
output "agent_ecr_repo_url" { value = module.agentcore.agent_ecr_repo_url }
output "agentcore_gateway_url" { value = module.agentcore.gateway_url }
output "agentcore_runtime_ids" { value = module.agentcore.runtime_ids }
output "cognito_issuer" { value = module.identity.issuer }
output "cognito_agent_client_id" { value = module.identity.agent_client_id }
output "cognito_approval_client_id" { value = module.identity.approval_client_id }
output "session_context_secret_arn" { value = module.identity.session_context_secret_arn }
output "demo_usernames" { value = module.identity.demo_usernames }

output "demo_passwords" {
  description = "Permanent Cognito passwords for the demo users. Read with tofu output demo_passwords. Single-step Hosted UI login; no forced password change."
  value       = module.identity.demo_passwords
  sensitive   = true
}

output "agentcore_summary" {
  description = "Native AgentCore resource and target-registration status."
  value       = module.agentcore.provisioning_summary
}
