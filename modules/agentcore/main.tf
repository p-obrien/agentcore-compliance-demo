# ---------------------------------------------------------------------------
# Amazon Bedrock AgentCore: Runtime (Intake + Assessment) + Gateway + Identity
# ---------------------------------------------------------------------------
# PROVIDER COVERAGE (verified against awscc v1.99 schema, 2026):
#
# Gateway, Runtime, Runtime Endpoint, and Workload Identity are native
# `awscc_bedrockagentcore_*` resources and are used directly below.
#
# The ONE exception is the Gateway *target* (the MCP tool registration that
# attaches the retrieval Lambda with an inline tool schema). Cloud Control does
# not expose a gateway-target resource type, so `awscc` has none. That single
# step uses `aws bedrock-agentcore-control create-gateway-target` via a
# null_resource with a matching destroy provisioner. Everything else is native.
#
# The isolation guarantee does NOT depend on any of this. Tenant/ACL
# enforcement lives in the retrieval Lambda (modules/retrieval_tool) and the
# Elastic DLS roles. AgentCore provides the identity/tool/session boundaries.

terraform {
  required_providers {
    aws   = { source = "hashicorp/aws", version = "~> 5.70" }
    awscc = { source = "hashicorp/awscc", version = "~> 1.20" }
    null  = { source = "hashicorp/null", version = "~> 3.2" }
  }
}

locals {
  gateway_name   = "${var.name_prefix}-gw"
  runtime_intake = "${replace(var.name_prefix, "-", "_")}_intake"
  runtime_assess = "${replace(var.name_prefix, "-", "_")}_assessment"

  # Runtimes are only created once a real image exists. Until then the whole
  # AgentCore layer (gateway, target, runtimes) is held back so the rest of the
  # lab still applies cleanly. Flip by passing a real agent_image_uri.
  image_ready = var.agent_image_uri != "PLACEHOLDER" && var.agent_image_uri != ""

  runtimes = {
    intake     = { name = local.runtime_intake, model = var.model_classification_id }
    assessment = { name = local.runtime_assess, model = var.model_assessment_id }
  }

  # Only the CLI-backed gateway target still needs a scratch dir for its id.
  state_dir = "${path.module}/.agentcore"
}

# --- ECR repo for the agent container image --------------------------------
resource "aws_ecr_repository" "agents" {
  name                 = "${var.name_prefix}-agents"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true # throwaway lab: allow destroy with images present

  image_scanning_configuration {
    scan_on_push = false
  }

  tags = var.tags
}

# --- IAM execution role AgentCore Runtime assumes --------------------------
resource "aws_iam_role" "runtime" {
  name                 = "${var.name_prefix}-agentcore-runtime"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = var.account_id }
      }
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "runtime" {
  name = "${var.name_prefix}-agentcore-runtime-policy"
  role = aws_iam_role.runtime.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream"
        ]
        # Scoped to the configured inference profiles and the foundation models
        # they route to (cross-region APAC profiles fan out across regions), not
        # Resource:"*". The guardrail ARN is covered by the ApplyGuardrail
        # statement below.
        Resource = concat(
          [
            "arn:aws:bedrock:*:${var.account_id}:inference-profile/${var.model_assessment_id}",
            "arn:aws:bedrock:*:${var.account_id}:inference-profile/${var.model_classification_id}",
            "arn:aws:bedrock:*:${var.account_id}:application-inference-profile/*",
          ],
          ["arn:aws:bedrock:*::foundation-model/*"]
        )
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:ApplyGuardrail"]
        Resource = var.guardrail_arn
      },
      {
        Effect = "Allow"
        Action = [
          "states:StartExecution"
        ]
        Resource = var.approval_state_machine_arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = var.audit_table_arn
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = var.session_context_secret_arn
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer"
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

# --- IAM role the Gateway assumes to reach targets (the Lambda) ------------
resource "aws_iam_role" "gateway" {
  name                 = "${var.name_prefix}-agentcore-gateway"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = var.account_id }
      }
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "gateway" {
  name = "${var.name_prefix}-agentcore-gateway-policy"
  role = aws_iam_role.gateway.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = var.retrieval_tool_function_arn
    }]
  })
}

# Allow the Gateway/Runtime service principal to invoke the retrieval Lambda.
resource "aws_lambda_permission" "gateway_invoke" {
  statement_id   = "AllowAgentCoreGatewayInvoke"
  action         = "lambda:InvokeFunction"
  function_name  = var.retrieval_tool_function_name
  principal      = "bedrock-agentcore.amazonaws.com"
  source_account = var.account_id
}

# --- Workload identity (native) --------------------------------------------
resource "awscc_bedrockagentcore_workload_identity" "this" {
  name = "${replace(var.name_prefix, "-", "_")}_wli"
}

# ---------------------------------------------------------------------------
# Gateway (MCP protocol) — native awscc resource.
# ---------------------------------------------------------------------------
resource "awscc_bedrockagentcore_gateway" "this" {
  name            = local.gateway_name
  role_arn        = aws_iam_role.gateway.arn
  protocol_type   = "MCP"
  authorizer_type = "CUSTOM_JWT"
  description     = "Authenticated retrieval and tool gateway for the isolation demo."

  authorizer_configuration = {
    custom_jwt_authorizer = {
      discovery_url   = "${var.cognito_issuer}/.well-known/openid-configuration"
      allowed_clients = [var.cognito_agent_client_id]
    }
  }

  protocol_configuration = {
    mcp = {
      instructions = "Permit-review tools. Tenant and ACL scope are enforced server-side and cannot be set by callers."
    }
  }

  tags = var.tags

  depends_on = [aws_iam_role_policy.gateway]
}

# ---------------------------------------------------------------------------
# Gateway target: retrieval Lambda registered as an MCP tool. Callers provide
# query, permit_id, and an opaque signed capability, never tenant or ACL scope.
#
# NO awscc resource exists for gateway targets (Cloud Control does not surface
# the type as of awscc 1.99). This single null_resource wraps the GA
# `create-gateway-target` API and has a matching destroy provisioner.
# ---------------------------------------------------------------------------
resource "null_resource" "gateway_target" {
  count = local.image_ready ? 1 : 0

  triggers = {
    gateway_id  = awscc_bedrockagentcore_gateway.this.gateway_identifier
    lambda_arn  = var.retrieval_tool_function_arn
    region      = var.region
    state_dir   = local.state_dir
    script_hash = filesha256("${path.module}/scripts/gateway_target.sh")
  }

  provisioner "local-exec" {
    when    = create
    command = "${path.module}/scripts/gateway_target.sh create"
    environment = {
      GW_ID         = awscc_bedrockagentcore_gateway.this.gateway_identifier
      LAMBDA_ARN    = var.retrieval_tool_function_arn
      REGION        = var.region
      STATE_DIR     = local.state_dir
      ALLOWED_TOOLS = "retrieval"
    }
  }

  provisioner "local-exec" {
    when    = destroy
    command = "${self.triggers.state_dir}/../scripts/gateway_target.sh destroy"
    environment = {
      GW_ID     = self.triggers.gateway_id
      REGION    = self.triggers.region
      STATE_DIR = self.triggers.state_dir
    }
  }
}

# ---------------------------------------------------------------------------
# Runtimes (native) — Intake and Assessment. Created only once a real image
# exists (local.image_ready); otherwise held back so the rest of the lab
# applies. Each gets an endpoint so it is invokable.
# ---------------------------------------------------------------------------
resource "awscc_bedrockagentcore_runtime" "this" {
  for_each = local.image_ready ? local.runtimes : {}

  agent_runtime_name = each.value.name
  role_arn           = aws_iam_role.runtime.arn
  description        = "AgentCore ${each.key} agent (Strands) for the isolation demo."

  agent_runtime_artifact = {
    container_configuration = {
      container_uri = var.agent_image_uri
    }
  }

  network_configuration = {
    network_mode = "PUBLIC"
  }

  protocol_configuration = "HTTP"

  authorizer_configuration = {
    custom_jwt_authorizer = {
      discovery_url   = "${var.cognito_issuer}/.well-known/openid-configuration"
      allowed_clients = [var.cognito_agent_client_id]
    }
  }

  # AgentCore consumes the inbound `Authorization` header for the CUSTOM_JWT
  # authorizer and does not forward it to the agent. The agent still needs the
  # access token (to verify subject match and to authenticate the Gateway
  # call), so the caller also sends it under `X-Access-Token`, which is
  # allowlisted here and forwarded to the agent. `X-Id-Token` carries the
  # companion ID token the same way.
  request_header_configuration = {
    request_header_allowlist = ["Authorization", "X-Id-Token", "X-Access-Token"]
  }

  environment_variables = {
    AGENT_KIND                 = each.key
    MODEL_ID                   = each.value.model
    GUARDRAIL_ID               = var.guardrail_id
    GUARDRAIL_VERSION          = var.guardrail_version
    GATEWAY_URL                = awscc_bedrockagentcore_gateway.this.gateway_url
    COGNITO_ISSUER             = var.cognito_issuer
    COGNITO_AGENT_CLIENT_ID    = var.cognito_agent_client_id
    SESSION_CONTEXT_SECRET_ARN = var.session_context_secret_arn
    APPROVAL_STATE_MACHINE_ARN = var.approval_state_machine_arn
    AUDIT_TABLE                = var.audit_table_name
    AWS_REGION_NAME            = var.region
  }

  tags = var.tags

  depends_on = [aws_iam_role_policy.runtime]
}

resource "awscc_bedrockagentcore_runtime_endpoint" "this" {
  for_each = local.image_ready ? local.runtimes : {}

  agent_runtime_id = awscc_bedrockagentcore_runtime.this[each.key].agent_runtime_id
  name             = "${each.value.name}_ep"
  description      = "Default endpoint for the ${each.key} runtime."
}
