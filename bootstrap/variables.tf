variable "aws_region" {
  description = "Region for the state bucket + lock table. Sydney."
  type        = string
  default     = "ap-southeast-2"
}

variable "aws_profile" {
  description = "AWS profile/SSO profile. Prefer setting AWS_PROFILE in the environment instead."
  type        = string
  default     = null
}

variable "name_prefix" {
  description = "Prefix for state resources. Keep in sync with the root module."
  type        = string
  default     = "agentcore-compliance-demo"
}

variable "tags" {
  type = map(string)
  default = {
    project    = "agentcore-compliance-demo"
    owner      = "agentcore-compliance-demo"
    managed_by = "opentofu"
    ephemeral  = "true"
  }
}
