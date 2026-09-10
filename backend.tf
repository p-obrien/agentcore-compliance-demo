# ---------------------------------------------------------------------------
# Remote state backend (S3 + DynamoDB lock)
# ---------------------------------------------------------------------------
# Left commented so the root can `tofu init` with local state during dev.
# To enable remote state:
#   1. cd bootstrap && tofu apply   (creates the bucket + lock table)
#   2. copy the `backend_config_snippet` output into this block below
#   3. uncomment and run `tofu init -migrate-state` in the root
#
# terraform {
#   backend "s3" {
#     bucket         = "agentcore-compliance-demo-tfstate-XXXXXXXX"
#     key            = "agentcore-compliance-demo/terraform.tfstate"
#     region         = "ap-southeast-2"
#     dynamodb_table = "agentcore-compliance-demo-tflock"
#     encrypt        = true
#   }
# }
