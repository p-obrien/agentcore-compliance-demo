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

variable "permissions_boundary_arn" {
  description = "Workload permissions boundary attached to the approval API Lambda role."
  type        = string
}
