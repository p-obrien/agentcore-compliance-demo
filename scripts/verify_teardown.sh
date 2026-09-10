#!/usr/bin/env bash
# Post-teardown residual verification.
#
# After `tofu destroy`, this queries the region for every resource type the lab
# creates. For each type that still has a resource, it prints one retry or
# manual-removal command and exits non-zero. Run from the repository root.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

REGION="${REGION:-ap-southeast-2}"
PREFIX="${PREFIX:-agentcore-compliance-demo}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo UNKNOWN)"
REMAINING=0

report() {
  # report <resource-type> <count> <manual-removal-command>
  local kind="$1" count="$2" cmd="$3"
  if [[ "${count:-0}" -gt 0 ]]; then
    REMAINING=1
    printf 'REMAINS  %-28s count=%s\n         remove: %s\n' "$kind" "$count" "$cmd"
  else
    printf 'CLEAN    %s\n' "$kind"
  fi
}

# Managed domains (shared + dedicated)
DOMAINS="$(aws opensearch list-domain-names --region "$REGION" --query "DomainNames[?starts_with(DomainName, 'acd')].DomainName" --output text 2>/dev/null | wc -w | tr -d ' ')"
report "opensearch_domains" "$DOMAINS" "aws opensearch delete-domain --region $REGION --domain-name <name>"

# AgentCore gateways
GW="$(aws bedrock-agentcore-control list-gateways --region "$REGION" --query "items[?starts_with(name, '${PREFIX}')].gatewayId" --output text 2>/dev/null | wc -w | tr -d ' ')"
report "agentcore_gateways" "$GW" "aws bedrock-agentcore-control delete-gateway --region $REGION --gateway-identifier <id>"

# AgentCore runtimes
RT="$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" --query "items[?starts_with(agentRuntimeName, '$(echo "$PREFIX" | tr '-' '_')')].agentRuntimeId" --output text 2>/dev/null | wc -w | tr -d ' ')"
report "agentcore_runtimes" "$RT" "aws bedrock-agentcore-control delete-agent-runtime --region $REGION --agent-runtime-id <id>"

# DynamoDB tables (audit, records, pending)
DDB="$(aws dynamodb list-tables --region "$REGION" --query "TableNames[?starts_with(@, '${PREFIX}')]" --output text 2>/dev/null | wc -w | tr -d ' ')"
report "dynamodb_tables" "$DDB" "aws dynamodb delete-table --region $REGION --table-name <name>"

# Step Functions state machine
SFN="$(aws stepfunctions list-state-machines --region "$REGION" --query "stateMachines[?starts_with(name, '${PREFIX}')].stateMachineArn" --output text 2>/dev/null | wc -w | tr -d ' ')"
report "step_functions" "$SFN" "aws stepfunctions delete-state-machine --region $REGION --state-machine-arn <arn>"

# Lambda functions
LAM="$(aws lambda list-functions --region "$REGION" --query "Functions[?starts_with(FunctionName, '${PREFIX}')].FunctionName" --output text 2>/dev/null | wc -w | tr -d ' ')"
report "lambda_functions" "$LAM" "aws lambda delete-function --region $REGION --function-name <name>"

# API Gateway HTTP APIs
API="$(aws apigatewayv2 get-apis --region "$REGION" --query "Items[?starts_with(Name, '${PREFIX}')].ApiId" --output text 2>/dev/null | wc -w | tr -d ' ')"
report "http_apis" "$API" "aws apigatewayv2 delete-api --region $REGION --api-id <id>"

# CloudFront distributions (global)
CF="$(aws cloudfront list-distributions --query "DistributionList.Items[?contains(Comment, '${PREFIX}')].Id" --output text 2>/dev/null | wc -w | tr -d ' ')"
report "cloudfront_distributions" "$CF" "aws cloudfront delete-distribution --id <id> --if-match <etag> (disable first)"

# ECR repository
ECR="$(aws ecr describe-repositories --region "$REGION" --query "repositories[?starts_with(repositoryName, '${PREFIX}')].repositoryName" --output text 2>/dev/null | wc -w | tr -d ' ')"
report "ecr_repositories" "$ECR" "aws ecr delete-repository --region $REGION --repository-name <name> --force"

# S3 buckets (site, bedrock logs, trail)
S3="$(aws s3api list-buckets --query "Buckets[?starts_with(Name, '${PREFIX}')].Name" --output text 2>/dev/null | wc -w | tr -d ' ')"
report "s3_buckets" "$S3" "aws s3 rb s3://<bucket> --force"

# VPC endpoints
VPCE="$(aws ec2 describe-vpc-endpoints --region "$REGION" --filters "Name=tag:Name,Values=${PREFIX}*" --query "VpcEndpoints[].VpcEndpointId" --output text 2>/dev/null | wc -w | tr -d ' ')"
report "vpc_endpoints" "$VPCE" "aws ec2 delete-vpc-endpoints --region $REGION --vpc-endpoint-ids <id>"

# Security groups
SG="$(aws ec2 describe-security-groups --region "$REGION" --filters "Name=group-name,Values=${PREFIX}*" --query "SecurityGroups[].GroupId" --output text 2>/dev/null | wc -w | tr -d ' ')"
report "security_groups" "$SG" "aws ec2 delete-security-group --region $REGION --group-id <id>"

# IAM roles for this lab
IAM="$(aws iam list-roles --query "Roles[?starts_with(RoleName, '${PREFIX}')].RoleName" --output text 2>/dev/null | wc -w | tr -d ' ')"
report "iam_roles" "$IAM" "aws iam delete-role --role-name <name> (detach policies first)"

echo
echo "Account $ACCOUNT / region $REGION"
if [[ "$REMAINING" -eq 0 ]]; then
  echo "Teardown verified: no lab resources remain."
else
  echo "Some resources remain. Re-run 'tofu destroy' or use the manual-removal commands above." >&2
  exit 1
fi
