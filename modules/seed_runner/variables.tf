variable "name_prefix" { type = string }

variable "tags" {
  type    = map(string)
  default = {}
}

variable "seed_targets" {
  description = "Per-tenant managed-domain endpoint and index map consumed by the seed loader."
  type = map(object({
    endpoint   = string
    index_name = string
    tier       = string
  }))
}

variable "seed_role_arn" {
  description = "Separate IAM role that can create indexes and write synthetic documents."
  type        = string
}

variable "vpc_subnet_ids" {
  description = "Private application subnet IDs for the seed-runner Lambda ENIs."
  type        = list(string)
}

variable "seed_security_group_id" {
  description = "seed-sg attached to the seed-runner Lambda ENIs."
  type        = string
}

variable "permissions_boundary_arn" {
  description = "Workload permissions boundary attached to the seed-runner role."
  type        = string
}
