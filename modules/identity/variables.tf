variable "name_prefix" { type = string }

variable "tags" {
  type    = map(string)
  default = {}
}

variable "approval_callback_url" {
  description = "CloudFront approval page URL used by Cognito Hosted UI redirect."
  type        = string
}

variable "region" { type = string }
