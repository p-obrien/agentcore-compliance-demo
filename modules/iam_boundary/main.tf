# ---------------------------------------------------------------------------
# Workload permissions boundary.
# ---------------------------------------------------------------------------
# Attached to every Lambda, AgentCore, and Step Functions role. It caps what
# those roles can ever do regardless of their inline policies: it allows the
# ordinary runtime action surface but explicitly denies IAM administration,
# KMS and resource-policy changes, OpenSearch domain configuration changes, and
# destructive data-plane writes to the audit table. Domain administration lives
# on a separate operator/admin role that does NOT carry this boundary.

terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.70" }
  }
}

resource "aws_iam_policy" "boundary" {
  name        = "${var.name_prefix}-workload-boundary"
  description = "Permissions boundary for lab workload roles (Lambda, AgentCore, Step Functions)."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Allow the ordinary runtime surface. Inline role policies still have to
        # grant a specific action; the boundary only caps the maximum.
        Sid    = "AllowRuntimeSurface"
        Effect = "Allow"
        Action = [
          "logs:*",
          "dynamodb:GetItem",
          "dynamodb:Query",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "es:ESHttpGet",
          "es:ESHttpPost",
          "es:ESHttpPut",
          "sts:AssumeRole",
          "secretsmanager:GetSecretValue",
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:ApplyGuardrail",
          "states:StartExecution",
          "states:SendTaskSuccess",
          "states:SendTaskFailure",
          "lambda:InvokeFunction",
          "ec2:CreateNetworkInterface",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DeleteNetworkInterface",
          "ecr:GetAuthorizationToken",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer"
        ]
        Resource = "*"
      },
      {
        # Deny IAM administration outright.
        Sid    = "DenyIamAdministration"
        Effect = "Deny"
        Action = [
          "iam:CreateRole",
          "iam:DeleteRole",
          "iam:AttachRolePolicy",
          "iam:DetachRolePolicy",
          "iam:PutRolePolicy",
          "iam:DeleteRolePolicy",
          "iam:CreatePolicy",
          "iam:CreatePolicyVersion",
          "iam:UpdateAssumeRolePolicy",
          "iam:CreateUser",
          "iam:CreateAccessKey",
          "iam:PassRole"
        ]
        Resource = "*"
      },
      {
        # Deny KMS key and resource-policy changes.
        Sid    = "DenyKmsAndResourcePolicyChanges"
        Effect = "Deny"
        Action = [
          "kms:CreateGrant",
          "kms:PutKeyPolicy",
          "kms:ScheduleKeyDeletion",
          "kms:DisableKey",
          "s3:PutBucketPolicy",
          "s3:DeleteBucketPolicy",
          "secretsmanager:PutResourcePolicy",
          "dynamodb:PutResourcePolicy",
          "lambda:AddPermission",
          "lambda:RemovePermission"
        ]
        Resource = "*"
      },
      {
        # Deny OpenSearch domain configuration changes; workloads only reach the
        # data plane (es:ESHttp*), never the control plane.
        Sid    = "DenyOpenSearchConfigChanges"
        Effect = "Deny"
        Action = [
          "es:CreateDomain",
          "es:DeleteDomain",
          "es:UpdateDomainConfig",
          "opensearch:CreateDomain",
          "opensearch:DeleteDomain",
          "opensearch:UpdateDomainConfig"
        ]
        Resource = "*"
      },
      {
        # Deny destructive data-plane writes to the audit table (append-only).
        Sid    = "DenyAuditMutation"
        Effect = "Deny"
        Action = [
          "dynamodb:DeleteItem",
          "dynamodb:DeleteTable",
          "dynamodb:UpdateTable"
        ]
        Resource = var.audit_table_arn
      }
    ]
  })

  tags = var.tags
}
