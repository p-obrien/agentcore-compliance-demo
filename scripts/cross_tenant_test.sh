#!/usr/bin/env bash
# Operator-only rehearsal of the retrieval Lambda's signed-capability contract.
# Runtime traffic uses AgentCore Gateway MCP; this direct invocation proves the
# same Lambda, tenant-scoped managed-domain read role, ACL filter, and spoof
# auditing without placing a Cognito password or session key in this repository.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

REGION="${REGION:-ap-southeast-2}"
FUNCTION_NAME="$(tofu output -raw retrieval_tool_function_name)"
SECRET_ARN="$(tofu output -raw session_context_secret_arn)"
SECRET_JSON="$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET_ARN" --query SecretString --output text)"

payload() {
  local tenant="$1"
  local query="$2"
  local permit_id="${3:-}"
  local spoof_tenant="${4:-}"
  local args=(--tenant "$tenant" --query "$query")
  [[ -n "$permit_id" ]] && args+=(--permit-id "$permit_id")
  [[ -n "$spoof_tenant" ]] && args+=(--spoof-tenant "$spoof_tenant")
  printf '%s' "$SECRET_JSON" | uv run --no-project python scripts/mint_session_token.py "${args[@]}"
}

invoke() {
  local label="$1"
  local event="$2"
  echo "=============================================================="
  echo "$label"
  aws lambda invoke \
    --region "$REGION" \
    --function-name "$FUNCTION_NAME" \
    --cli-binary-format raw-in-base64-out \
    --payload "$event" \
    /tmp/retrieval_out.json >/dev/null
  uv run --no-project python -c 'import json; d=json.load(open("/tmp/retrieval_out.json")); print("count =", d.get("count")); print("effective_tenant =", d.get("tenant_id")); print("permits =", [r.get("permit_id") for r in d.get("results", [])]); print("reason =", d.get("reason", ""))'
  echo
}

invoke "1) Agency A normal query, expect Agency A permits" "$(payload agency-a permit)"
invoke "2) Agency B normal query, expect Agency B permits" "$(payload agency-b permit)"
invoke "3) Agency C normal query, expect Agency C permits (dedicated tier)" "$(payload agency-c permit)"
invoke "4a) Agency A requests Agency B permit B-2001, expect count = 0" "$(payload agency-a '' B-2001)"
invoke "4b) Agency A spoofs tenant_id=agency-b, effective tenant remains Agency A" "$(payload agency-a permit '' agency-b)"

echo "The final call must show only Agency A permits and is audited as tenant_spoof_attempt_ignored."
echo "Query the audit table with:"
echo "  aws dynamodb query --region $REGION --table-name \$(tofu output -raw audit_table_name) \\\"
echo "    --index-name by-tenant --key-condition-expression 'tenant_id = :t' \\\"
echo "    --expression-attribute-values '{\":t\":{\"S\":\"agency-a\"}}'"
