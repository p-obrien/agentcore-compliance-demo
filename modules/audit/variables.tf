variable "name_prefix" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "account_id" {
  type = string
}

variable "region" {
  type = string
}

variable "permissions_boundary_arn" {
  description = "Workload permissions boundary attached to the trace-read Lambda role."
  type        = string
}
