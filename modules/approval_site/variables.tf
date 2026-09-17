variable "name_prefix" { type = string }

variable "tags" {
  type    = map(string)
  default = {}
}

variable "pending_table_name" { type = string }
variable "pending_table_arn" { type = string }
variable "audit_table_name" { type = string }
variable "audit_table_arn" { type = string }
variable "cognito_issuer" { type = string }
variable "cognito_approval_client_id" { type = string }
variable "cognito_hosted_ui_domain" { type = string }

variable "state_machine_arn" {
  description = "Approval Step Functions state machine ARN. The approval API resumes only this one, so states:SendTaskSuccess/Failure is scoped to it."
  type        = string
}

variable "throttle_burst_limit" {
  description = "API Gateway stage burst limit (concurrent requests). Guards the model-backed approval path from an authenticated caller or leaked token."
  type        = number
  default     = 20
}

variable "throttle_rate_limit" {
  description = "API Gateway stage steady-state request rate (requests/second)."
  type        = number
  default     = 10
}

variable "permissions_boundary_arn" {
  description = "Workload permissions boundary attached to the approval API Lambda role."
  type        = string
}
