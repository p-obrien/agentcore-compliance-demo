output "boundary_policy_arn" {
  description = "Permissions-boundary managed policy ARN to attach to every workload role."
  value       = aws_iam_policy.boundary.arn
}
