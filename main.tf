# Root composition. The data flow is authenticated identity -> signed tenant
# context -> Gateway retrieval -> guarded assessment -> Step Functions approval
# -> mock write-back, with an append-only audit record at every boundary.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id          = data.aws_caller_identity.current.account_id
  region              = data.aws_region.current.name
  tenant_ids          = ["agency-a", "agency-b", "agency-c"]
  retrieval_role_name = "${var.name_prefix}-retrieval-tool-role"
}

# --- VPC, private subnets, and security groups ------------------------------
module "network" {
  source      = "./modules/network"
  name_prefix = var.name_prefix
  tags        = var.tags
}

# --- Audit first: the permissions boundary references the audit table ARN ---
module "audit" {
  source                   = "./modules/audit"
  name_prefix              = var.name_prefix
  tags                     = var.tags
  account_id               = local.account_id
  region                   = local.region
  permissions_boundary_arn = module.iam_boundary.boundary_policy_arn
}

module "iam_boundary" {
  source          = "./modules/iam_boundary"
  name_prefix     = var.name_prefix
  tags            = var.tags
  audit_table_arn = module.audit.audit_table_arn
}

# --- Two in-VPC managed domains (shared A/B, dedicated C) -------------------
module "opensearch" {
  source = "./modules/opensearch"

  name_prefix                         = var.name_prefix
  tags                                = var.tags
  account_id                          = local.account_id
  region                              = local.region
  retrieval_role_name                 = local.retrieval_role_name
  tenant_ids                          = local.tenant_ids
  vpc_subnet_ids                      = module.network.private_subnet_ids
  opensearch_domain_security_group_id = module.network.opensearch_domain_security_group_id
  fgac_config_security_group_id       = module.network.seed_security_group_id
  permissions_boundary_arn            = module.iam_boundary.boundary_policy_arn
}

module "guardrails" {
  source      = "./modules/guardrails"
  name_prefix = var.name_prefix
  tags        = var.tags
}

module "approval_flow" {
  source                   = "./modules/approval_flow"
  name_prefix              = var.name_prefix
  tags                     = var.tags
  audit_table_name         = module.audit.audit_table_name
  audit_table_arn          = module.audit.audit_table_arn
  permissions_boundary_arn = module.iam_boundary.boundary_policy_arn
}

# CloudFront itself does not depend on the rendered site object. This output is
# therefore safe to use as the Cognito Hosted UI callback without a cycle.
module "approval_site" {
  source = "./modules/approval_site"

  name_prefix                = var.name_prefix
  tags                       = var.tags
  pending_table_name         = module.approval_flow.pending_table_name
  pending_table_arn          = module.approval_flow.pending_table_arn
  audit_table_name           = module.audit.audit_table_name
  audit_table_arn            = module.audit.audit_table_arn
  cognito_issuer             = module.identity.issuer
  cognito_approval_client_id = module.identity.approval_client_id
  cognito_hosted_ui_domain   = module.identity.hosted_ui_domain
  permissions_boundary_arn   = module.iam_boundary.boundary_policy_arn
}

module "identity" {
  source = "./modules/identity"

  name_prefix           = var.name_prefix
  tags                  = var.tags
  region                = local.region
  approval_callback_url = module.approval_site.cloudfront_url
}

module "retrieval_tool" {
  source = "./modules/retrieval_tool"

  name_prefix                 = var.name_prefix
  tags                        = var.tags
  managed_domain_targets      = module.opensearch.managed_domain_targets
  session_context_secret_arn  = module.identity.session_context_secret_arn
  vpc_subnet_ids              = module.network.private_subnet_ids
  retrieval_security_group_id = module.network.retrieval_security_group_id
  audit_table_name            = module.audit.audit_table_name
  audit_table_arn             = module.audit.audit_table_arn
  permissions_boundary_arn    = module.iam_boundary.boundary_policy_arn
}

# Seeding must execute from the VPC because both OpenSearch endpoints are
# private. The runner assumes the separate write role; it has no direct domain
# data-plane permission of its own.
module "seed_runner" {
  source = "./modules/seed_runner"

  name_prefix              = var.name_prefix
  tags                     = var.tags
  seed_targets             = module.opensearch.seed_targets
  seed_role_arn            = module.opensearch.seed_role_arn
  vpc_subnet_ids           = module.network.private_subnet_ids
  seed_security_group_id   = module.network.seed_security_group_id
  permissions_boundary_arn = module.iam_boundary.boundary_policy_arn
}

module "agentcore" {
  source = "./modules/agentcore"

  name_prefix                  = var.name_prefix
  tags                         = var.tags
  account_id                   = local.account_id
  region                       = local.region
  retrieval_tool_function_arn  = module.retrieval_tool.function_arn
  retrieval_tool_function_name = module.retrieval_tool.function_name
  guardrail_id                 = module.guardrails.guardrail_id
  guardrail_arn                = module.guardrails.guardrail_arn
  guardrail_version            = module.guardrails.guardrail_version
  model_assessment_id          = var.model_assessment_id
  model_classification_id      = var.model_classification_id
  tenant_ids                   = local.tenant_ids
  approval_state_machine_arn   = module.approval_flow.state_machine_arn
  audit_table_name             = module.audit.audit_table_name
  audit_table_arn              = module.audit.audit_table_arn
  session_context_secret_arn   = module.identity.session_context_secret_arn
  cognito_issuer               = module.identity.issuer
  cognito_agent_client_id      = module.identity.agent_client_id
  permissions_boundary_arn     = module.iam_boundary.boundary_policy_arn
  agent_image_uri              = var.agent_image_uri
}
