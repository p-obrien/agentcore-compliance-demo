output "user_pool_id" { value = aws_cognito_user_pool.this.id }
output "issuer" { value = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.this.id}" }
output "agent_client_id" { value = aws_cognito_user_pool_client.agent.id }
output "approval_client_id" { value = aws_cognito_user_pool_client.approval.id }
output "hosted_ui_domain" { value = "https://${aws_cognito_user_pool_domain.this.domain}.auth.${var.region}.amazoncognito.com" }
output "session_context_secret_arn" { value = aws_secretsmanager_secret.session_context.arn }
output "demo_usernames" { value = { for k, u in aws_cognito_user.demo : k => u.username } }
output "demo_passwords" {
  value     = { for k, p in random_password.demo : k => p.result }
  sensitive = true
}
