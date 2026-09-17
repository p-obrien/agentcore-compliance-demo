variable "name_prefix" { type = string }

variable "tags" {
  type    = map(string)
  default = {}
}

variable "account_id" {
  description = "AWS account owning the managed domains and IAM roles."
  type        = string
}

variable "region" {
  description = "AWS region hosting the managed domains (ap-southeast-2)."
  type        = string
  default     = "ap-southeast-2"
}

variable "retrieval_role_name" {
  description = "Deterministic retrieval Lambda execution role name trusted by tenant read roles."
  type        = string
}

variable "index_name" {
  description = "Index created inside every domain by the seed bootstrap role."
  type        = string
  default     = "permits"
}

variable "embedding_dims" {
  description = "knn_vector dimensions. 1024 matches Titan Text Embeddings v2."
  type        = number
  default     = 1024
}

variable "tenant_ids" {
  description = "Tenant identifiers. Agencies A and B share one domain; Agency C is dedicated."
  type        = list(string)
  default     = ["agency-a", "agency-b", "agency-c"]
}

variable "vpc_subnet_ids" {
  description = "Private application subnet IDs for the managed-domain VPC options."
  type        = list(string)
}

variable "opensearch_domain_security_group_id" {
  description = "Security group (opensearch-domain-sg) attached to both domains' ENIs."
  type        = string
}

variable "instance_type" {
  description = "Data node instance type for both domains."
  type        = string
  default     = "t3.small.search"
}

variable "instance_count" {
  description = "Data node count per domain. One AZ, one node keeps the disposable lab cheap."
  type        = number
  default     = 1
}

variable "engine_version" {
  description = "Amazon OpenSearch Service engine version."
  type        = string
  default     = "OpenSearch_3.7"
}

variable "fgac_config_security_group_id" {
  description = "seed-sg attached to the private Lambda that reconciles OpenSearch Security roles."
  type        = string
}

variable "permissions_boundary_arn" {
  description = "Workload permissions boundary attached to the FGAC reconciler execution role."
  type        = string
}
