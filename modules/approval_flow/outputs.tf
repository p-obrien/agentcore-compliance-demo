output "state_machine_arn" {
  value = aws_sfn_state_machine.this.arn
}

output "records_table_name" {
  value = aws_dynamodb_table.records.name
}

output "records_table_arn" {
  value = aws_dynamodb_table.records.arn
}

output "pending_table_name" {
  value = aws_dynamodb_table.pending.name
}

output "pending_table_arn" {
  value = aws_dynamodb_table.pending.arn
}
