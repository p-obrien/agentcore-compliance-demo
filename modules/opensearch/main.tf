# ---------------------------------------------------------------------------
# Two in-VPC Amazon OpenSearch Service managed domains.
# ---------------------------------------------------------------------------
# Shared tier (Agencies A and B): one domain. The Intelligence API injects the
# tenant_id filter and document-ACL clauses server-side; FGAC is an independent
# second evaluation, not a substitute for the filter.
#
# Dedicated tier (Agency C): a separate domain whose resource-based access
# policy DENIES the shared retrieval principal on es:ESHttp*. If the shared-tier
# query filter ever regresses, the shared principal is still refused before any
# Agency C document can be read.
#
# Three data-plane roles are created here: shared read (A/B), dedicated read (C),
# and seed. The FGAC master user is a separate operator/admin role that is kept
# out of every runtime/retrieval/seed/Gateway/agent policy.
# ---------------------------------------------------------------------------

terraform {
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.70" }
    archive = { source = "hashicorp/archive", version = "~> 2.6" }
  }
}

locals {
  # Domain names: lowercase, <= 28 chars, must start with a letter.
  domain_prefix    = "acd${substr(sha1(var.name_prefix), 0, 8)}"
  shared_domain    = "${local.domain_prefix}-shared"
  dedicated_domain = "${local.domain_prefix}-dedic"

  retrieval_principal_arn = "arn:aws:iam::${var.account_id}:role/${var.retrieval_role_name}"

  shared_domain_arn    = "arn:aws:es:${var.region}:${var.account_id}:domain/${local.shared_domain}"
  dedicated_domain_arn = "arn:aws:es:${var.region}:${var.account_id}:domain/${local.dedicated_domain}"

  # Per-tenant target contract consumed by the retrieval handler. A/B -> shared,
  # C -> dedicated. None of these values are secrets.
  managed_domain_targets = {
    "agency-a" = {
      endpoint        = "https://${aws_opensearch_domain.shared.endpoint}"
      index_name      = var.index_name
      role_arn        = aws_iam_role.shared_read.arn
      tier            = "shared"
      principal_class = "shared-retrieval"
      domain_arn      = local.shared_domain_arn
    }
    "agency-b" = {
      endpoint        = "https://${aws_opensearch_domain.shared.endpoint}"
      index_name      = var.index_name
      role_arn        = aws_iam_role.shared_read.arn
      tier            = "shared"
      principal_class = "shared-retrieval"
      domain_arn      = local.shared_domain_arn
    }
    "agency-c" = {
      endpoint        = "https://${aws_opensearch_domain.dedicated.endpoint}"
      index_name      = var.index_name
      role_arn        = aws_iam_role.dedicated_read.arn
      tier            = "dedicated"
      principal_class = "dedicated-retrieval"
      domain_arn      = local.dedicated_domain_arn
    }
  }
}

# --- FGAC master / operator-admin role (held only by the operator) ----------
resource "aws_iam_role" "domain_admin" {
  name = "${var.name_prefix}-os-domain-admin"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${var.account_id}:root" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

# --- Data-plane read roles, assumed only by the verified retrieval Lambda ----
resource "aws_iam_role" "shared_read" {
  name = "${var.name_prefix}-os-shared-read"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${var.account_id}:root" }
      Action    = "sts:AssumeRole"
      Condition = { ArnEquals = { "aws:PrincipalArn" = local.retrieval_principal_arn } }
    }]
  })
  tags = merge(var.tags, { tier = "shared" })
}

resource "aws_iam_role" "dedicated_read" {
  name = "${var.name_prefix}-os-dedicated-read"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${var.account_id}:root" }
      Action    = "sts:AssumeRole"
      Condition = { ArnEquals = { "aws:PrincipalArn" = local.retrieval_principal_arn } }
    }]
  })
  tags = merge(var.tags, { tier = "dedicated" })
}

resource "aws_iam_role" "seed" {
  name = "${var.name_prefix}-os-seed"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${var.account_id}:root" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

# Data-plane HTTP permissions. The shared read role reaches only the shared
# domain; the dedicated read role reaches only the dedicated domain.
resource "aws_iam_role_policy" "shared_read" {
  name = "${var.name_prefix}-os-shared-read-policy"
  role = aws_iam_role.shared_read.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["es:ESHttpGet", "es:ESHttpPost"]
      Resource = "${local.shared_domain_arn}/*"
    }]
  })
}

resource "aws_iam_role_policy" "dedicated_read" {
  name = "${var.name_prefix}-os-dedicated-read-policy"
  role = aws_iam_role.dedicated_read.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["es:ESHttpGet", "es:ESHttpPost"]
      Resource = "${local.dedicated_domain_arn}/*"
    }]
  })
}

resource "aws_iam_role_policy" "seed" {
  name = "${var.name_prefix}-os-seed-policy"
  role = aws_iam_role.seed.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["es:ESHttpGet", "es:ESHttpPost", "es:ESHttpPut"]
      Resource = [
        "${local.shared_domain_arn}/*",
        "${local.dedicated_domain_arn}/*",
      ]
    }]
  })
}

# --- Shared-tier managed domain --------------------------------------------
resource "aws_opensearch_domain" "shared" {
  domain_name    = local.shared_domain
  engine_version = var.engine_version

  cluster_config {
    instance_type  = var.instance_type
    instance_count = var.instance_count
  }

  ebs_options {
    ebs_enabled = true
    volume_size = 10
    volume_type = "gp3"
  }

  vpc_options {
    subnet_ids         = [var.vpc_subnet_ids[0]]
    security_group_ids = [var.opensearch_domain_security_group_id]
  }

  encrypt_at_rest { enabled = true }
  node_to_node_encryption { enabled = true }
  domain_endpoint_options {
    enforce_https       = true
    tls_security_policy = "Policy-Min-TLS-1-2-2019-07"
  }

  advanced_security_options {
    enabled                        = true
    internal_user_database_enabled = false
    master_user_options {
      master_user_arn = aws_iam_role.domain_admin.arn
    }
  }

  # Shared read and seed roles are permitted; the dedicated read role is not.
  access_policies = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = [aws_iam_role.shared_read.arn, aws_iam_role.seed.arn, aws_iam_role.domain_admin.arn] }
      Action    = "es:ESHttp*"
      Resource  = "${local.shared_domain_arn}/*"
    }]
  })

  tags = merge(var.tags, { tier = "shared" })
}

# --- Dedicated-tier managed domain (Agency C) ------------------------------
resource "aws_opensearch_domain" "dedicated" {
  domain_name    = local.dedicated_domain
  engine_version = var.engine_version

  cluster_config {
    instance_type  = var.instance_type
    instance_count = var.instance_count
  }

  ebs_options {
    ebs_enabled = true
    volume_size = 10
    volume_type = "gp3"
  }

  vpc_options {
    subnet_ids         = [var.vpc_subnet_ids[0]]
    security_group_ids = [var.opensearch_domain_security_group_id]
  }

  encrypt_at_rest { enabled = true }
  node_to_node_encryption { enabled = true }
  domain_endpoint_options {
    enforce_https       = true
    tls_security_policy = "Policy-Min-TLS-1-2-2019-07"
  }

  advanced_security_options {
    enabled                        = true
    internal_user_database_enabled = false
    master_user_options {
      master_user_arn = aws_iam_role.domain_admin.arn
    }
  }

  # The dedicated-domain access policy permits the dedicated read, seed, and
  # admin roles, and includes an EXPLICIT deny for the shared retrieval role.
  # The deny is the access-control boundary that holds even if a shared-tier
  # query filter regresses.
  access_policies = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowDedicatedPrincipals"
        Effect    = "Allow"
        Principal = { AWS = [aws_iam_role.dedicated_read.arn, aws_iam_role.seed.arn, aws_iam_role.domain_admin.arn] }
        Action    = "es:ESHttp*"
        Resource  = "${local.dedicated_domain_arn}/*"
      },
      {
        Sid       = "DenySharedRetrievalPrincipal"
        Effect    = "Deny"
        Principal = { AWS = aws_iam_role.shared_read.arn }
        Action    = "es:ESHttp*"
        Resource  = "${local.dedicated_domain_arn}/*"
      },
    ]
  })

  tags = merge(var.tags, { tier = "dedicated" })
}

# --- Fine-grained access-control role reconciliation ------------------------
# IAM and domain access policies admit a request to the domain. The OpenSearch
# Security plugin then evaluates backend-role mappings independently. These
# role definitions are owned by Terraform and deliberately narrow: readers can
# search only permits; seed can check/create the index, index documents, and
# refresh it. The shared reader represents both A and B, so the verified
# tenant_id query filter remains their per-agency boundary.
locals {
  fgac_spec = {
    domains = [
      {
        endpoint = "https://${aws_opensearch_domain.shared.endpoint}"
        roles = [
          {
            name = "agentcore_permits_shared_reader"
            definition = {
              cluster_permissions = []
              index_permissions = [{
                index_patterns  = [var.index_name]
                allowed_actions = ["indices:data/read/search"]
              }]
              tenant_permissions = []
            }
            backend_roles = [aws_iam_role.shared_read.arn]
          },
          {
            name = "agentcore_permits_shared_seed_writer"
            definition = {
              # OpenSearch authorizes the bulk dispatch that backs a single
              # PUT /_doc at the CLUSTER scope, then the per-document write at
              # the index scope. Granting bulk only on the index pattern fails
              # the cluster-level check, so it must live in cluster_permissions.
              cluster_permissions = ["indices:data/write/bulk"]
              index_permissions = [{
                index_patterns = [var.index_name]
                allowed_actions = [
                  # GET /permits checks whether the index exists and returns a
                  # Security error body when authorization is incomplete.
                  "indices:admin/get",
                  "indices:admin/create",
                  "indices:data/write/index",
                  "indices:data/write/bulk*",
                  # _refresh fans out to a shard-level sub-action
                  # (indices:admin/refresh[s]); the wildcard covers both.
                  "indices:admin/refresh*",
                ]
              }]
              tenant_permissions = []
            }
            backend_roles = [aws_iam_role.seed.arn]
          },
        ]
      },
      {
        endpoint = "https://${aws_opensearch_domain.dedicated.endpoint}"
        roles = [
          {
            name = "agentcore_permits_dedicated_reader"
            definition = {
              cluster_permissions = []
              index_permissions = [{
                index_patterns  = [var.index_name]
                allowed_actions = ["indices:data/read/search"]
                dls             = jsonencode({ term = { tenant_id = "agency-c" } })
              }]
              tenant_permissions = []
            }
            backend_roles = [aws_iam_role.dedicated_read.arn]
          },
          {
            name = "agentcore_permits_dedicated_seed_writer"
            definition = {
              # See the shared seed writer: bulk is a cluster-scoped action.
              cluster_permissions = ["indices:data/write/bulk"]
              index_permissions = [{
                index_patterns = [var.index_name]
                allowed_actions = [
                  # GET /permits checks whether the index exists and returns a
                  # Security error body when authorization is incomplete.
                  "indices:admin/get",
                  "indices:admin/create",
                  "indices:data/write/index",
                  "indices:data/write/bulk*",
                  # _refresh fans out to a shard-level sub-action
                  # (indices:admin/refresh[s]); the wildcard covers both.
                  "indices:admin/refresh*",
                ]
              }]
              tenant_permissions = []
            }
            backend_roles = [aws_iam_role.seed.arn]
          },
        ]
      },
    ]
  }

  fgac_function_name = "${var.name_prefix}-os-fgac-configurator"
}

data "archive_file" "fgac_configurator" {
  type        = "zip"
  source_dir  = "${path.module}/src"
  output_path = "${path.module}/build/fgac_configurator.zip"
  excludes    = ["__pycache__"]
}

resource "aws_iam_role" "fgac_configurator" {
  name                 = "${local.fgac_function_name}-role"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "fgac_configurator" {
  name = "${local.fgac_function_name}-policy"
  role = aws_iam_role.fgac_configurator.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect = "Allow"
        Action = [
          "ec2:CreateNetworkInterface",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DeleteNetworkInterface",
        ]
        Resource = "*"
      },
      {
        # It obtains OpenSearch Security API authority only through the
        # existing FGAC master role and has no direct es:ESHttp* permission.
        Effect   = "Allow"
        Action   = ["sts:AssumeRole"]
        Resource = aws_iam_role.domain_admin.arn
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "fgac_configurator" {
  name              = "/aws/lambda/${local.fgac_function_name}"
  retention_in_days = 7
  tags              = var.tags
}

resource "aws_lambda_function" "fgac_configurator" {
  function_name    = local.fgac_function_name
  role             = aws_iam_role.fgac_configurator.arn
  runtime          = "python3.12"
  handler          = "fgac_configurator.handler"
  filename         = data.archive_file.fgac_configurator.output_path
  source_code_hash = data.archive_file.fgac_configurator.output_base64sha256
  timeout          = 180
  memory_size      = 256

  vpc_config {
    subnet_ids         = var.vpc_subnet_ids
    security_group_ids = [var.fgac_config_security_group_id]
  }

  environment {
    variables = {
      DOMAIN_ADMIN_ROLE_ARN = aws_iam_role.domain_admin.arn
      FGAC_SPEC_JSON        = jsonencode(local.fgac_spec)
    }
  }

  depends_on = [aws_cloudwatch_log_group.fgac_configurator]
  tags       = var.tags
}

# Invoke synchronously as part of apply. A failed FGAC reconciliation prevents
# Make from reaching the seed runner, and trigger changes reapply the complete
# Terraform-owned role and mapping definition.
#
# CREATE_ONLY, not CRUD: the reconcile only matters while the domains exist. A
# delete-time invocation would run during destroy, after the destroy graph has
# already started removing the STS interface endpoint and the Lambda's SG, so it
# times out reaching STS from the now-isolated subnet and aborts `make destroy`.
# The domains are being deleted anyway, so there is nothing to reconcile on the
# way down.
resource "aws_lambda_invocation" "fgac" {
  function_name   = aws_lambda_function.fgac_configurator.function_name
  input           = jsonencode({ operation = "reconcile" })
  lifecycle_scope = "CREATE_ONLY"
  triggers = {
    code_hash = data.archive_file.fgac_configurator.output_base64sha256
    spec_hash = sha256(jsonencode(local.fgac_spec))
  }

  depends_on = [
    aws_opensearch_domain.shared,
    aws_opensearch_domain.dedicated,
    aws_iam_role_policy.shared_read,
    aws_iam_role_policy.dedicated_read,
    aws_iam_role_policy.seed,
  ]
}
