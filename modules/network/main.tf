# ---------------------------------------------------------------------------
# Network foundation for the in-VPC managed-domain lab.
# ---------------------------------------------------------------------------
# agentcore-demo-vpc holds two private application subnets in distinct AZs. The
# managed OpenSearch domains attach ENIs into these subnets, and the retrieval
# Lambda attaches here too. There is no internet-facing domain endpoint.
#
# Egress is an operator decision (var.egress_strategy):
#   - vpc_endpoints (default): interface/gateway VPC endpoints reach AWS APIs
#     over PrivateLink; the private route tables have no default route.
#   - nat_gateway: a single NAT gateway in a public subnet gives the private
#     subnets an internet path when the AgentCore Runtime needs one.

terraform {
  required_version = ">= 1.8.0"

  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.70" }
  }
}

data "aws_region" "current" {}

locals {
  use_nat        = var.egress_strategy == "nat_gateway"
  use_endpoints  = var.egress_strategy == "vpc_endpoints"
  enable_diag_sg = var.enable_facilitator_diagnostic_path

  # Index-aligned AZ/CIDR pairs keep the two private subnets deterministic.
  private_subnets = {
    "a" = { az = var.availability_zones[0], cidr = var.private_subnet_cidrs[0] }
    "b" = { az = var.availability_zones[1], cidr = var.private_subnet_cidrs[1] }
  }

  interface_endpoints = local.use_endpoints ? toset(var.interface_endpoint_services) : toset([])
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  instance_tenancy     = "default"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(var.tags, { Name = "agentcore-demo-vpc" })
}

# ---------------------------------------------------------------------------
# Private application subnets (two AZs).
# ---------------------------------------------------------------------------
resource "aws_subnet" "private" {
  for_each = local.private_subnets

  vpc_id            = aws_vpc.this.id
  cidr_block        = each.value.cidr
  availability_zone = each.value.az

  # No public IPs. These subnets never receive an internet-facing endpoint.
  map_public_ip_on_launch = false

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-private-${each.key}"
    tier = "private-app"
  })
}

resource "aws_route_table" "private" {
  for_each = local.private_subnets

  vpc_id = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-private-rt-${each.key}" })
}

resource "aws_route_table_association" "private" {
  for_each = local.private_subnets

  subnet_id      = aws_subnet.private[each.key].id
  route_table_id = aws_route_table.private[each.key].id
}

# ---------------------------------------------------------------------------
# NAT egress path (only when egress_strategy = "nat_gateway").
# ---------------------------------------------------------------------------
resource "aws_internet_gateway" "this" {
  count = local.use_nat ? 1 : 0

  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name_prefix}-igw" })
}

resource "aws_subnet" "public" {
  count = local.use_nat ? 1 : 0

  vpc_id                  = aws_vpc.this.id
  cidr_block              = var.public_subnet_cidr
  availability_zone       = var.availability_zones[0]
  map_public_ip_on_launch = false

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-public-nat"
    tier = "public-nat"
  })
}

resource "aws_route_table" "public" {
  count = local.use_nat ? 1 : 0

  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name_prefix}-public-rt" })
}

resource "aws_route" "public_internet" {
  count = local.use_nat ? 1 : 0

  route_table_id         = aws_route_table.public[0].id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this[0].id
}

resource "aws_route_table_association" "public" {
  count = local.use_nat ? 1 : 0

  subnet_id      = aws_subnet.public[0].id
  route_table_id = aws_route_table.public[0].id
}

resource "aws_eip" "nat" {
  count = local.use_nat ? 1 : 0

  domain = "vpc"
  tags   = merge(var.tags, { Name = "${var.name_prefix}-nat-eip" })

  depends_on = [aws_internet_gateway.this]
}

resource "aws_nat_gateway" "this" {
  count = local.use_nat ? 1 : 0

  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[0].id

  tags = merge(var.tags, { Name = "${var.name_prefix}-nat" })

  depends_on = [aws_internet_gateway.this]
}

# Default route from each private subnet through the single NAT gateway.
resource "aws_route" "private_nat" {
  for_each = local.use_nat ? local.private_subnets : {}

  route_table_id         = aws_route_table.private[each.key].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[0].id
}

# ---------------------------------------------------------------------------
# Security groups.
# ---------------------------------------------------------------------------

# opensearch-domain-sg: the network gate in front of both managed domains.
# It only allows HTTPS from the retrieval and seed SGs (and the optional
# facilitator diagnostic SG). Rules that reference other SGs are declared as
# standalone rules to avoid a create-time cycle between the SGs.
resource "aws_security_group" "opensearch_domain" {
  name        = "${var.name_prefix}-opensearch-domain-sg"
  description = "Managed OpenSearch domain ENIs. HTTPS only from retrieval and seed SGs."
  vpc_id      = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-opensearch-domain-sg" })
}

# retrieval-sg: the only workload path that reads either domain. No inbound.
resource "aws_security_group" "retrieval" {
  name        = "${var.name_prefix}-retrieval-sg"
  description = "Retrieval Lambda ENIs. No inbound; HTTPS egress to domains and AWS endpoints."
  vpc_id      = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-retrieval-sg" })
}

# seed-sg: seed runner / CodeBuild / operator EC2. No inbound.
resource "aws_security_group" "seed" {
  name        = "${var.name_prefix}-seed-sg"
  description = "Seed runner. No inbound; HTTPS egress to the managed domains only."
  vpc_id      = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-seed-sg" })
}

# Optional facilitator diagnostic SG for read-only domain diagnostics.
resource "aws_security_group" "facilitator_diagnostic" {
  count = local.enable_diag_sg ? 1 : 0

  name        = "${var.name_prefix}-facilitator-diag-sg"
  description = "Optional bastion/proxy path for facilitator read-only domain diagnostics."
  vpc_id      = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-facilitator-diag-sg" })
}

# --- opensearch-domain-sg ingress (443 from retrieval, seed, optional diag) ---
resource "aws_vpc_security_group_ingress_rule" "domain_from_retrieval" {
  security_group_id            = aws_security_group.opensearch_domain.id
  description                  = "HTTPS from retrieval-sg"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.retrieval.id
}

resource "aws_vpc_security_group_ingress_rule" "domain_from_seed" {
  security_group_id            = aws_security_group.opensearch_domain.id
  description                  = "HTTPS from seed-sg"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.seed.id
}

resource "aws_vpc_security_group_ingress_rule" "domain_from_diagnostic" {
  count = local.enable_diag_sg ? 1 : 0

  security_group_id            = aws_security_group.opensearch_domain.id
  description                  = "HTTPS from facilitator diagnostic SG"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.facilitator_diagnostic[0].id
}

# --- retrieval-sg egress (443 to domain SG and to AWS endpoints) ---
resource "aws_vpc_security_group_egress_rule" "retrieval_to_domain" {
  security_group_id            = aws_security_group.retrieval.id
  description                  = "HTTPS to opensearch-domain-sg"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.opensearch_domain.id
}

# HTTPS to the interface VPC endpoints (or NAT path) for required AWS services.
resource "aws_vpc_security_group_egress_rule" "retrieval_to_endpoints" {
  security_group_id = aws_security_group.retrieval.id
  description       = "HTTPS to required AWS VPC endpoints / AWS APIs"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = var.vpc_cidr
}

# DynamoDB is a GATEWAY endpoint reached over the AWS service prefix list, whose
# addresses are public AWS ranges OUTSIDE the VPC CIDR. The vpc_cidr egress rule
# above does not cover them, so without this rule the retrieval Lambda's audit
# put_item connect-times-out on dynamodb.<region>.amazonaws.com. Allow 443 to
# the DynamoDB prefix list when egress is via VPC endpoints.
resource "aws_vpc_security_group_egress_rule" "retrieval_to_dynamodb" {
  count = local.use_endpoints ? 1 : 0

  security_group_id = aws_security_group.retrieval.id
  description       = "HTTPS to DynamoDB gateway endpoint prefix list"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  prefix_list_id    = aws_vpc_endpoint.dynamodb[0].prefix_list_id
}

# --- seed-sg egress (443 to domain SG and required AWS APIs) ---
resource "aws_vpc_security_group_egress_rule" "seed_to_domain" {
  security_group_id            = aws_security_group.seed.id
  description                  = "HTTPS to opensearch-domain-sg"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.opensearch_domain.id
}

# The runner assumes the seed role through STS, emits logs, and creates Titan
# embeddings. With PrivateLink those services resolve inside the VPC; the NAT
# option intentionally permits HTTPS egress through its explicitly selected
# outbound path instead.
resource "aws_vpc_security_group_egress_rule" "seed_to_aws_apis" {
  security_group_id = aws_security_group.seed.id
  description       = "HTTPS to required AWS VPC endpoints / AWS APIs"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = local.use_endpoints ? var.vpc_cidr : "0.0.0.0/0"
}

# --- facilitator diagnostic SG egress + operator ingress (optional) ---
resource "aws_vpc_security_group_egress_rule" "diagnostic_to_domain" {
  count = local.enable_diag_sg ? 1 : 0

  security_group_id            = aws_security_group.facilitator_diagnostic[0].id
  description                  = "HTTPS to opensearch-domain-sg"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.opensearch_domain.id
}

resource "aws_vpc_security_group_ingress_rule" "diagnostic_from_operator" {
  for_each = local.enable_diag_sg ? toset(var.facilitator_diagnostic_cidrs) : toset([])

  security_group_id = aws_security_group.facilitator_diagnostic[0].id
  description       = "HTTPS from operator/bastion CIDR"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = each.value
}

# ---------------------------------------------------------------------------
# VPC endpoints (only when egress_strategy = "vpc_endpoints").
# ---------------------------------------------------------------------------
# Interface endpoints get ENIs in the private subnets and reuse retrieval-sg for
# ingress from the workload. The S3 gateway endpoint covers ECR image layers.
resource "aws_security_group" "endpoints" {
  count = local.use_endpoints ? 1 : 0

  name        = "${var.name_prefix}-vpce-sg"
  description = "Interface VPC endpoint ENIs. HTTPS from private workload SGs."
  vpc_id      = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-vpce-sg" })
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_retrieval" {
  count = local.use_endpoints ? 1 : 0

  security_group_id            = aws_security_group.endpoints[0].id
  description                  = "HTTPS from retrieval-sg"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.retrieval.id
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_seed" {
  count = local.use_endpoints ? 1 : 0

  security_group_id            = aws_security_group.endpoints[0].id
  description                  = "HTTPS from seed-sg"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.seed.id
}

resource "aws_vpc_endpoint" "interface" {
  for_each = local.interface_endpoints

  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${data.aws_region.current.name}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [for k in keys(local.private_subnets) : aws_subnet.private[k].id]
  security_group_ids  = [aws_security_group.endpoints[0].id]
  private_dns_enabled = true

  tags = merge(var.tags, { Name = "${var.name_prefix}-vpce-${replace(each.value, ".", "-")}" })
}

# Gateway endpoints attach AWS service prefix-list routes directly to private
# route tables. DynamoDB does not support Interface endpoint private DNS in this
# configuration, so it must not be included in `interface_endpoint_services`.
resource "aws_vpc_endpoint" "s3" {
  count = local.use_endpoints ? 1 : 0

  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [for k in keys(local.private_subnets) : aws_route_table.private[k].id]

  tags = merge(var.tags, { Name = "${var.name_prefix}-vpce-s3" })
}

resource "aws_vpc_endpoint" "dynamodb" {
  count = local.use_endpoints ? 1 : 0

  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [for k in keys(local.private_subnets) : aws_route_table.private[k].id]

  tags = merge(var.tags, { Name = "${var.name_prefix}-vpce-dynamodb" })
}
