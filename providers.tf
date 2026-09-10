# ---------------------------------------------------------------------------
# Provider configuration. The lab uses AWS credentials only.
# ---------------------------------------------------------------------------

provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile

  default_tags {
    tags = var.tags
  }
}

provider "awscc" {
  region  = var.aws_region
  profile = var.aws_profile
}
