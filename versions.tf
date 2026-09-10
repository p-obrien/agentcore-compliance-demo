terraform {
  # OpenTofu. Use the `tofu` binary, not `terraform`.
  required_version = ">= 1.8.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }

    # AWS Cloud Control provider. Used for newer services (including some
    # Bedrock AgentCore resources) the classic AWS provider does not expose.
    awscc = {
      source  = "hashicorp/awscc"
      version = "~> 1.20"
    }

    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }

    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.6"
    }

    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }
}
