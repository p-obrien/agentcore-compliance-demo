variable "name_prefix" { type = string }

variable "tags" {
  type    = map(string)
  default = {}
}

variable "managed_domain_targets" {
  description = <<-EOT
    Per-tenant managed-domain retrieval target. None are credentials.
    Agencies A and B resolve to the shared domain; Agency C resolves to the
    dedicated domain. The handler resolves tier, endpoint, index, assume-role
    ARN, and principal class from this map keyed by the verified tenant only.
  EOT
  type = map(object({
    endpoint        = string
    index_name      = string
    role_arn        = string
    tier            = string
    principal_class = string
    domain_arn      = string
  }))
}

variable "session_context_secret_arn" {
  description = "Shared runtime/Lambda secret used to verify signed session context."
  type        = string
}

variable "vpc_subnet_ids" {
  description = "Private application subnet IDs for the retrieval Lambda VPC attachment."
  type        = list(string)
}

variable "retrieval_security_group_id" {
  description = "Security group (retrieval-sg) attached to the retrieval Lambda ENIs."
  type        = string
}

variable "audit_table_name" { type = string }
variable "audit_table_arn" { type = string }

variable "permissions_boundary_arn" {
  description = "Workload permissions boundary attached to the retrieval Lambda role."
  type        = string
}
