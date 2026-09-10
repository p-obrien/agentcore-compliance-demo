output "managed_domain_endpoints" {
  description = "Shared and dedicated managed-domain HTTPS endpoints. Access requires SigV4 and the matching domain access policy."
  value = {
    shared    = "https://${aws_opensearch_domain.shared.endpoint}"
    dedicated = "https://${aws_opensearch_domain.dedicated.endpoint}"
  }
}

output "managed_domain_targets" {
  description = "Per-tenant retrieval target contract (non-secret). A/B -> shared, C -> dedicated."
  value       = local.managed_domain_targets
}

output "seed_targets" {
  description = "Non-secret per-tenant endpoint/index for the seed role. A/B -> shared, C -> dedicated."
  value = {
    for tenant, target in local.managed_domain_targets : tenant => {
      endpoint   = target.endpoint
      index_name = target.index_name
      tier       = target.tier
    }
  }
}

output "shared_retrieval_role_arn" {
  description = "Shared-tier (Agencies A and B) data-plane read role ARN."
  value       = aws_iam_role.shared_read.arn
}

output "dedicated_retrieval_role_arn" {
  description = "Dedicated-tier (Agency C) data-plane read role ARN."
  value       = aws_iam_role.dedicated_read.arn
}

output "seed_role_arn" {
  description = "Seed-only role with write access to both managed domains."
  value       = aws_iam_role.seed.arn
}

output "domain_admin_role_arn" {
  description = "FGAC master / operator-admin role. Kept out of every runtime/retrieval/seed/Gateway/agent policy."
  value       = aws_iam_role.domain_admin.arn
}
