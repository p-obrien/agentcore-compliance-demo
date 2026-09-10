output "vpc_id" {
  description = "ID of agentcore-demo-vpc."
  value       = aws_vpc.this.id
}

output "vpc_cidr" {
  description = "IPv4 CIDR of the lab VPC."
  value       = aws_vpc.this.cidr_block
}

output "private_subnet_ids" {
  description = "IDs of the two private application subnets, for managed-domain VPC options and the retrieval Lambda VPC attachment."
  value       = [for k in sort(keys(aws_subnet.private)) : aws_subnet.private[k].id]
}

output "private_subnet_ids_by_az" {
  description = "Private subnet IDs keyed by Availability Zone."
  value       = { for k, s in aws_subnet.private : s.availability_zone => s.id }
}

output "availability_zones" {
  description = "Availability Zones hosting the private application subnets."
  value       = [for k in sort(keys(aws_subnet.private)) : aws_subnet.private[k].availability_zone]
}

output "opensearch_domain_security_group_id" {
  description = "opensearch-domain-sg. Attach to both managed domains' VPC options."
  value       = aws_security_group.opensearch_domain.id
}

output "retrieval_security_group_id" {
  description = "retrieval-sg. Attach to the retrieval Lambda ENIs."
  value       = aws_security_group.retrieval.id
}

output "seed_security_group_id" {
  description = "seed-sg. Attach to the seed runner / CodeBuild / operator EC2."
  value       = aws_security_group.seed.id
}

output "facilitator_diagnostic_security_group_id" {
  description = "Optional facilitator diagnostic SG ID, or null when the diagnostic path is disabled."
  value       = local.enable_diag_sg ? aws_security_group.facilitator_diagnostic[0].id : null
}

output "egress_strategy" {
  description = "Resolved egress strategy in effect for the VPC."
  value       = var.egress_strategy
}

output "interface_vpc_endpoint_ids" {
  description = "Interface VPC endpoint IDs keyed by short service name. Empty when egress_strategy = \"nat_gateway\"."
  value       = { for svc, ep in aws_vpc_endpoint.interface : svc => ep.id }
}

output "s3_gateway_endpoint_id" {
  description = "S3 gateway VPC endpoint ID, or null when egress_strategy = \"nat_gateway\"."
  value       = local.use_endpoints ? aws_vpc_endpoint.s3[0].id : null
}

output "nat_gateway_id" {
  description = "NAT gateway ID, or null when egress_strategy = \"vpc_endpoints\"."
  value       = local.use_nat ? aws_nat_gateway.this[0].id : null
}
