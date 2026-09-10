variable "name_prefix" { type = string }

variable "tags" {
  type    = map(string)
  default = {}
}

variable "account_id" { type = string }
variable "region" { type = string }
variable "retrieval_tool_function_arn" { type = string }
variable "retrieval_tool_function_name" { type = string }
variable "guardrail_id" { type = string }
variable "guardrail_arn" { type = string }
variable "guardrail_version" { type = string }
variable "model_assessment_id" { type = string }
variable "model_classification_id" { type = string }
variable "tenant_ids" { type = list(string) }
variable "approval_state_machine_arn" { type = string }
variable "audit_table_name" { type = string }
variable "audit_table_arn" { type = string }
variable "session_context_secret_arn" { type = string }
variable "cognito_issuer" { type = string }
variable "cognito_agent_client_id" { type = string }

variable "permissions_boundary_arn" {
  description = "Workload permissions boundary attached to the runtime and gateway roles."
  type        = string
}

variable "agent_image_uri" {
  description = "Digest-pinned ECR image URI for the Strands agent container. PLACEHOLDER holds runtimes back."
  type        = string
  default     = "PLACEHOLDER"

  validation {
    condition     = var.agent_image_uri == "PLACEHOLDER" || can(regex("@sha256:[0-9a-f]{64}$", var.agent_image_uri))
    error_message = "agent_image_uri must be PLACEHOLDER or an immutable image URI ending in @sha256:<64 lowercase hex characters>."
  }
}
