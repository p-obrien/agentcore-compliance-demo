# ---------------------------------------------------------------------------
# Approval flow: Step Functions (waitForTaskToken) + write-back + tables
# ---------------------------------------------------------------------------

terraform {
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.70" }
    archive = { source = "hashicorp/archive", version = "~> 2.6" }
  }
}

# --- pending approvals table (holds the task token while suspended) --------
# status transitions: PENDING_APPROVAL -> DECIDING -> {APPROVED|REJECTED|COMMIT_FAILED}
# The task token is retained only while PENDING_APPROVAL or DECIDING; terminal
# transitions clear it. by_tenant_status backs the tenant-scoped Approval API
# listing; by_interaction links a proposal to its Interaction_ID trace.
resource "aws_dynamodb_table" "pending" {
  name         = "${var.name_prefix}-pending-approvals"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "approval_id"

  attribute {
    name = "approval_id"
    type = "S"
  }
  attribute {
    name = "tenant_id"
    type = "S"
  }
  attribute {
    name = "status"
    type = "S"
  }
  attribute {
    name = "interaction_id"
    type = "S"
  }

  global_secondary_index {
    name            = "by_tenant_status"
    hash_key        = "tenant_id"
    range_key       = "status"
    projection_type = "ALL"
  }

  global_secondary_index {
    name            = "by_interaction"
    hash_key        = "interaction_id"
    projection_type = "ALL"
  }

  tags = var.tags
}

# --- mock write-back record store (nothing lands until approval) -----------
resource "aws_dynamodb_table" "records" {
  name         = "${var.name_prefix}-records"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "record_id"

  attribute {
    name = "record_id"
    type = "S"
  }

  tags = var.tags
}

# --- Lambdas ----------------------------------------------------------------
data "archive_file" "register_pending" {
  type        = "zip"
  source_dir  = "${path.module}/src/register_pending"
  output_path = "${path.module}/build/register_pending.zip"
}

data "archive_file" "write_back" {
  type        = "zip"
  source_dir  = "${path.module}/src/write_back"
  output_path = "${path.module}/build/write_back.zip"
}

resource "aws_iam_role" "register_pending" {
  name                 = "${var.name_prefix}-register-pending-role"
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

# register_pending attaches the task token to the agent-created proposal and
# writes audit. It has NO records-table permission: only the write-back Lambda
# may write the Mock_Record_Store.
resource "aws_iam_role_policy" "register_pending" {
  name = "${var.name_prefix}-register-pending-policy"
  role = aws_iam_role.register_pending.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.pending.arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = var.audit_table_arn
      }
    ]
  })
}

resource "aws_iam_role" "write_back" {
  name                 = "${var.name_prefix}-write-back-role"
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

# write_back is the ONLY principal permitted to write the records table. It also
# updates the pending proposal's terminal status and appends audit.
resource "aws_iam_role_policy" "write_back" {
  name = "${var.name_prefix}-write-back-policy"
  role = aws_iam_role.write_back.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = aws_dynamodb_table.records.arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.pending.arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = var.audit_table_arn
      }
    ]
  })
}

resource "aws_lambda_function" "register_pending" {
  function_name    = "${var.name_prefix}-register-pending"
  role             = aws_iam_role.register_pending.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.register_pending.output_path
  source_code_hash = data.archive_file.register_pending.output_base64sha256
  timeout          = 15

  environment {
    variables = {
      PENDING_TABLE = aws_dynamodb_table.pending.name
      AUDIT_TABLE   = var.audit_table_name
    }
  }
  tags = var.tags
}

resource "aws_lambda_function" "write_back" {
  function_name    = "${var.name_prefix}-write-back"
  role             = aws_iam_role.write_back.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.write_back.output_path
  source_code_hash = data.archive_file.write_back.output_base64sha256
  timeout          = 15

  environment {
    variables = {
      RECORDS_TABLE = aws_dynamodb_table.records.name
      PENDING_TABLE = aws_dynamodb_table.pending.name
      AUDIT_TABLE   = var.audit_table_name
    }
  }
  tags = var.tags
}

# --- Step Functions state machine ------------------------------------------
resource "aws_iam_role" "sfn" {
  name                 = "${var.name_prefix}-sfn-role"
  permissions_boundary = var.permissions_boundary_arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "sfn" {
  name = "${var.name_prefix}-sfn-policy"
  role = aws_iam_role.sfn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = [aws_lambda_function.register_pending.arn, aws_lambda_function.write_back.arn]
    }]
  })
}

resource "aws_sfn_state_machine" "this" {
  name     = "${var.name_prefix}-approval"
  role_arn = aws_iam_role.sfn.arn

  definition = templatefile("${path.module}/state_machine.asl.json", {
    register_pending_function_arn = aws_lambda_function.register_pending.arn
    write_back_function_arn       = aws_lambda_function.write_back.arn
  })

  tags = var.tags
}
