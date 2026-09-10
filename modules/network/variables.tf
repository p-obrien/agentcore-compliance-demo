variable "name_prefix" {
  description = "Prefix for all resource names."
  type        = string
}

variable "tags" {
  description = "Per-resource tags. Root default_tags already apply; use this for resource-specific keys."
  type        = map(string)
  default     = {}
}

variable "vpc_cidr" {
  description = "IPv4 CIDR block for agentcore-demo-vpc."
  type        = string
  default     = "10.20.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid IPv4 CIDR block."
  }
}

variable "availability_zones" {
  description = "Two Availability Zones for the private application subnets. Must be distinct."
  type        = list(string)
  default     = ["ap-southeast-2a", "ap-southeast-2b"]

  validation {
    condition     = length(var.availability_zones) == 2 && length(distinct(var.availability_zones)) == 2
    error_message = "Provide exactly two distinct Availability Zones."
  }
}

variable "private_subnet_cidrs" {
  description = "CIDR blocks for the two private application subnets, aligned by index with availability_zones."
  type        = list(string)
  default     = ["10.20.0.0/24", "10.20.1.0/24"]

  validation {
    condition     = length(var.private_subnet_cidrs) == 2 && alltrue([for c in var.private_subnet_cidrs : can(cidrhost(c, 0))])
    error_message = "Provide exactly two valid IPv4 CIDR blocks for the private subnets."
  }
}

variable "egress_strategy" {
  description = <<-EOT
    Egress path for private resources. "vpc_endpoints" (default) reaches AWS APIs
    and the managed-domain endpoints over PrivateLink with no internet path.
    "nat_gateway" provisions a single NAT gateway when the AgentCore Runtime needs
    egress through a private path. This is the NAT vs VPC-endpoint operator decision.
  EOT
  type        = string
  default     = "vpc_endpoints"

  validation {
    condition     = contains(["vpc_endpoints", "nat_gateway"], var.egress_strategy)
    error_message = "egress_strategy must be either \"vpc_endpoints\" or \"nat_gateway\"."
  }
}

variable "public_subnet_cidr" {
  description = "CIDR for the single public subnet that hosts the NAT gateway. Only used when egress_strategy = \"nat_gateway\"."
  type        = string
  default     = "10.20.255.0/24"

  validation {
    condition     = can(cidrhost(var.public_subnet_cidr, 0))
    error_message = "public_subnet_cidr must be a valid IPv4 CIDR block."
  }
}

variable "interface_endpoint_services" {
  description = <<-EOT
    Short service names for interface VPC endpoints the private workload needs when
    egress_strategy = "vpc_endpoints". The module prefixes each with
    com.amazonaws.<region>. OpenSearch data-plane traffic reaches the managed-domain
    ENIs directly inside the VPC and does not need an endpoint here.
  EOT
  type        = list(string)
  default = [
    "bedrock-runtime",
    "logs",
    "sts",
    "states",
    "ecr.api",
    "ecr.dkr",
    "secretsmanager",
  ]
}

variable "enable_facilitator_diagnostic_path" {
  description = "When true, opensearch-domain-sg accepts HTTPS from the facilitator diagnostic security group for read-only domain diagnostics."
  type        = bool
  default     = false
}

variable "facilitator_diagnostic_cidrs" {
  description = "Operator/bastion CIDR blocks allowed to reach the facilitator diagnostic security group over HTTPS. Only used when enable_facilitator_diagnostic_path = true."
  type        = list(string)
  default     = []
}
