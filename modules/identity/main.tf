# Cognito identity for authenticated agent sessions and approval decisions.
# Tenant is an immutable custom attribute in a verified ID token. The runtime
# validates the access token at ingress; agent code verifies the companion ID
# token before deriving a signed, short-lived retrieval context.

terraform {
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.70" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

locals {
  demo_user_ids = toset(["agency-a", "agency-b", "agency-c", "demo-operator"])
}

resource "random_string" "domain_suffix" {
  length  = 8
  upper   = false
  special = false
}

resource "aws_cognito_user_pool" "this" {
  name                     = "${var.name_prefix}-users"
  username_attributes      = ["email"]
  auto_verified_attributes = []

  password_policy {
    minimum_length    = 16
    require_lowercase = true
    require_numbers   = true
    require_symbols   = true
    require_uppercase = true
  }

  schema {
    attribute_data_type      = "String"
    developer_only_attribute = false
    mutable                  = false
    name                     = "tenant_id"
    required                 = false
    string_attribute_constraints {
      min_length = 1
      max_length = 64
    }
  }

  tags = var.tags
}

resource "aws_cognito_user_pool_domain" "this" {
  domain       = "${var.name_prefix}-${random_string.domain_suffix.result}"
  user_pool_id = aws_cognito_user_pool.this.id
}

resource "aws_cognito_user_pool_client" "agent" {
  name                                 = "${var.name_prefix}-agent-client"
  user_pool_id                         = aws_cognito_user_pool.this.id
  generate_secret                      = false
  prevent_user_existence_errors        = "ENABLED"
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_PASSWORD_AUTH", "ALLOW_USER_SRP_AUTH"]
  allowed_oauth_flows_user_pool_client = false
  access_token_validity                = 60
  id_token_validity                    = 60
  token_validity_units {
    access_token = "minutes"
    id_token     = "minutes"
  }
}

resource "aws_cognito_user_pool_client" "approval" {
  name                                 = "${var.name_prefix}-approval-client"
  user_pool_id                         = aws_cognito_user_pool.this.id
  generate_secret                      = false
  prevent_user_existence_errors        = "ENABLED"
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  callback_urls                        = [var.approval_callback_url]
  logout_urls                          = [var.approval_callback_url]
  supported_identity_providers         = ["COGNITO"]
  access_token_validity                = 60
  id_token_validity                    = 60
  token_validity_units {
    access_token = "minutes"
    id_token     = "minutes"
  }
}

# Tenant-scoped approver groups replace the former global demo-approvers group.
# An approver holds one or more <tenant>-approvers groups; the Approval API
# derives the tenant scope from these claims.
resource "aws_cognito_user_group" "approvers" {
  for_each     = toset(["agency-a-approvers", "agency-b-approvers", "agency-c-approvers"])
  name         = each.value
  user_pool_id = aws_cognito_user_pool.this.id
}

resource "aws_cognito_user_group" "tenant" {
  for_each     = toset(["agency-a-assessors", "agency-b-assessors", "agency-c-assessors"])
  name         = each.value
  user_pool_id = aws_cognito_user_pool.this.id
}

resource "random_password" "demo" {
  for_each         = local.demo_user_ids
  length           = 24
  special          = true
  override_special = "!#$%*+-_"
}

resource "aws_cognito_user" "demo" {
  for_each     = local.demo_user_ids
  user_pool_id = aws_cognito_user_pool.this.id
  username     = "${each.key}@example.invalid"

  attributes = each.key == "demo-operator" ? {} : {
    "custom:tenant_id" = each.key
  }

  temporary_password       = random_password.demo[each.key].result
  desired_delivery_mediums = []
  force_alias_creation     = false
  message_action           = "SUPPRESS"

  # Cognito adds immutable attributes such as `sub` after creation. Reconciling
  # the attributes map later would attempt to remove those fields or reapply
  # immutable `custom:tenant_id`, both of which Cognito rejects. Tenant changes
  # require a user replacement, not an in-place attribute update.
  lifecycle {
    ignore_changes = [attributes]
  }
}

resource "aws_cognito_user_in_group" "tenant" {
  for_each = {
    "agency-a" = "agency-a-assessors"
    "agency-b" = "agency-b-assessors"
    "agency-c" = "agency-c-assessors"
  }
  user_pool_id = aws_cognito_user_pool.this.id
  username     = aws_cognito_user.demo[each.key].username
  group_name   = aws_cognito_user_group.tenant[each.value].name
}

# The demo operator is an approver for all three tenants in this two-tier lab.
# A production deployment would scope approvers per tenant.
resource "aws_cognito_user_in_group" "operator" {
  for_each     = aws_cognito_user_group.approvers
  user_pool_id = aws_cognito_user_pool.this.id
  username     = aws_cognito_user.demo["demo-operator"].username
  group_name   = each.value.name
}

# Shared HMAC key. Only the runtime execution role and retrieval Lambda role
# can read it. The signed token binds a verified subject, tenant, ACL groups,
# and short expiry; the model never receives it.
resource "random_password" "session_context" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "session_context" {
  name                    = "${var.name_prefix}-session-context-key"
  recovery_window_in_days = 0
  tags                    = var.tags
}

resource "aws_secretsmanager_secret_version" "session_context" {
  secret_id     = aws_secretsmanager_secret.session_context.id
  secret_string = jsonencode({ key = random_password.session_context.result })
}
