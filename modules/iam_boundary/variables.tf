variable "name_prefix" { type = string }

variable "tags" {
  type    = map(string)
  default = {}
}

variable "audit_table_arn" {
  description = "Audit table ARN; the boundary denies destructive writes to it."
  type        = string
}
