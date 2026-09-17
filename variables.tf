# ---------------------------------------------------------------------------
# Root input variables
# ---------------------------------------------------------------------------

variable "aws_region" {
  description = "AWS region for all resources. Sydney only for this lab."
  type        = string
  default     = "ap-southeast-2"

  validation {
    condition     = var.aws_region == "ap-southeast-2"
    error_message = "This lab is Sydney-only. Keep aws_region = ap-southeast-2."
  }
}

variable "aws_profile" {
  description = "Named AWS CLI/SSO profile. Set it locally or through AWS_PROFILE; do not commit it."
  type        = string
  default     = null
}

variable "name_prefix" {
  description = "Prefix for all resource names."
  type        = string
  default     = "agentcore-compliance-demo"

  validation {
    condition     = can(regex("^[a-z0-9-]+$", var.name_prefix))
    error_message = "name_prefix must be lowercase letters, digits, and hyphens only."
  }
}

variable "tags" {
  description = "Cost-allocation and lifecycle tags applied to all taggable resources."
  type        = map(string)
  default = {
    project    = "agentcore-compliance-demo"
    owner      = "agentcore-compliance-demo"
    managed_by = "opentofu"
    ephemeral  = "true"
  }
}

variable "model_assessment_id" {
  description = "Australia cross-region inference profile for assessment."
  type        = string
  # au.anthropic.claude-sonnet-5 is the latest Australia Sonnet profile and is
  # ACTIVE in ap-southeast-2. The older apac.* Sonnet 4.5 profile does not exist
  # here (Bedrock rejects it as an invalid model identifier).
  default = "au.anthropic.claude-sonnet-5"
}

variable "model_classification_id" {
  description = "Australia cross-region inference profile for intake classification."
  type        = string
  # Latest Australia Haiku profile (no Haiku 5 exists yet).
  default = "au.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "agent_image_uri" {
  description = "Digest-pinned ECR image URI produced by agents/build.sh. PLACEHOLDER skips runtime creation."
  type        = string
  default     = "PLACEHOLDER"

  validation {
    condition     = var.agent_image_uri == "PLACEHOLDER" || can(regex("@sha256:[0-9a-f]{64}$", var.agent_image_uri))
    error_message = "agent_image_uri must be PLACEHOLDER or an immutable URI ending in @sha256:<64 lowercase hex characters>."
  }
}

variable "adopt_existing_ecr_repo" {
  description = "ECR repo name to import into state when it exists in AWS but is untracked (set by make deploy). Empty means create normally."
  type        = string
  default     = ""
}

variable "enable_scheduled_destroy" {
  description = "Reserved for an optional scheduled teardown. Off by default."
  type        = bool
  default     = false
}
