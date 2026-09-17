variable "name_prefix" { type = string }

variable "tags" {
  type    = map(string)
  default = {}
}

variable "audit_table_name" { type = string }
variable "audit_table_arn" { type = string }

variable "audit_by_tenant_index" {
  description = "Name of the audit table GSI keyed by tenant_id, read for the live audit panel."
  type        = string
  default     = "by-tenant"
}

variable "assessment_runtime_arn" {
  description = "Assessment AgentCore runtime ARN the demo API invokes. Empty until an agent image is applied; the API deploys either way."
  type        = string
  default     = ""
}

variable "cognito_issuer" { type = string }
variable "cognito_agent_client_id" { type = string }
variable "cognito_hosted_ui_domain" { type = string }

variable "approval_page_url" {
  description = "CloudFront URL of the existing approval page, linked from the demo site for the approval handoff."
  type        = string
}

variable "permissions_boundary_arn" {
  description = "Workload permissions boundary attached to the demo API Lambda role."
  type        = string
}
