variable "name_prefix" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "audit_table_name" {
  type = string
}

variable "audit_table_arn" {
  type = string
}

variable "permissions_boundary_arn" {
  description = "Workload permissions boundary attached to the approval Lambda and Step Functions roles."
  type        = string
}
