output "agent_ecr_repo_url" {
  description = "Push the agent image here (agents/build.sh), then re-apply with agent_image_uri."
  value       = aws_ecr_repository.agents.repository_url
}

output "gateway_name" {
  value = local.gateway_name
}

output "gateway_id" {
  value = awscc_bedrockagentcore_gateway.this.gateway_identifier
}

output "gateway_url" {
  value = awscc_bedrockagentcore_gateway.this.gateway_url
}

output "runtime_role_arn" {
  value = aws_iam_role.runtime.arn
}

output "gateway_role_arn" {
  value = aws_iam_role.gateway.arn
}

output "runtime_ids" {
  description = "AgentCore runtime ids (empty until a real agent image is applied)."
  value       = { for k, r in awscc_bedrockagentcore_runtime.this : k => r.agent_runtime_id }
}

output "provisioning_summary" {
  description = "How AgentCore was provisioned, surfaced at the root for operator clarity."
  value = join("\n", [
    "AgentCore provisioned with native awscc resources: Gateway (${local.gateway_name}, MCP), Workload Identity, and Runtimes + Endpoints.",
    "In-scope tools: retrieval. Out-of-scope (denied on purpose): record_writeback.",
    "Runtimes: ${local.runtime_intake}, ${local.runtime_assess}.",
    local.image_ready ? "Runtimes and the gateway target are created (agent_image_uri set)." : "Runtimes + gateway target are HELD BACK until agent_image_uri is set (run agents/build.sh, then re-apply).",
    "Gateway target (MCP tool registration) is the only CLI-backed step: no awscc resource type exists for it.",
  ])
}
