#!/usr/bin/env bash
# Register gateway MCP tool targets.
#
# This is the ONE AgentCore step with no awscc resource type (Cloud Control does
# not surface gateway targets). Everything else in modules/agentcore is native.
# The gateway id is passed in from the native awscc_bedrockagentcore_gateway
# resource, so this script never has to resolve it by name.
#
# Two targets are registered:
#   - retrieval:    the in-scope tenant-isolated retrieval tool. Accepts
#                   model-visible query and permit_id plus a required opaque
#                   capability minted by the authenticated runtime. The Gateway
#                   inline schema supports only its documented field subset, so
#                   tenant scope is enforced independently in the handler; it
#                   ignores and audits caller-supplied scope-shaped fields.
#   - mock-backend: a DELIBERATELY out-of-scope target. It is registered on the
#                   gateway but is NOT in the agent's allowed-tool scope, so an
#                   agent attempt to invoke it is denied. This backs the
#                   out-of-scope-tool denial demonstration (requirement 6.8).
set -euo pipefail

ACTION="${1:?usage: gateway_target.sh create|destroy}"
: "${GW_ID:?}"
: "${REGION:?}"
: "${STATE_DIR:?}"

mkdir -p "$STATE_DIR"
TGT_ID_FILE="$STATE_DIR/target_${GW_ID}.id"
MOCK_TGT_ID_FILE="$STATE_DIR/mock_target_${GW_ID}.id"

case "$ACTION" in
  create)
    : "${LAMBDA_ARN:?}"
    : "${ALLOWED_TOOLS:?}"

    if [[ -f "$TGT_ID_FILE" ]] && [[ -n "$(cat "$TGT_ID_FILE")" ]]; then
      echo "Gateway target already recorded ($(cat "$TGT_ID_FILE")); reusing."
    else
      target_config="$(cat <<JSON
{
  "mcp": {
    "lambda": {
      "lambdaArn": "${LAMBDA_ARN}",
      "toolSchema": {
        "inlinePayload": [
          {
            "name": "retrieval",
            "description": "Search permit documents the current session is authorised to see. Tenant and ACL scope are enforced server-side from session context and cannot be set by the caller.",
            "inputSchema": {
              "type": "object",
              "properties": {
                "query": { "type": "string", "description": "Optional free-text search over permit documents." },
                "permit_id": { "type": "string", "description": "Optional exact permit id to fetch." },
                "session_token": { "type": "string", "description": "Opaque, short-lived session capability supplied by the authenticated agent runtime. Do not construct or alter it." }
              },
              "required": ["session_token"]
            }
          }
        ]
      }
    }
  }
}
JSON
)"

      tid="$(aws bedrock-agentcore-control create-gateway-target \
        --region "$REGION" \
        --gateway-identifier "$GW_ID" \
        --name "retrieval-tool" \
        --target-configuration "$target_config" \
        --credential-provider-configurations '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]' \
        --query 'targetId' --output text)"
      echo "$tid" > "$TGT_ID_FILE"
      echo "Registered retrieval target ${tid} on gateway ${GW_ID} (allowed tools: ${ALLOWED_TOOLS})."
    fi

    # Deliberately out-of-scope mock backend target. Registered but not in the
    # agent's allowed-tool scope, so invoking it is denied in the demo.
    if [[ -f "$MOCK_TGT_ID_FILE" ]] && [[ -n "$(cat "$MOCK_TGT_ID_FILE")" ]]; then
      echo "Mock-backend target already recorded ($(cat "$MOCK_TGT_ID_FILE")); reusing."
      exit 0
    fi

    mock_config="$(cat <<JSON
{
  "mcp": {
    "lambda": {
      "lambdaArn": "${LAMBDA_ARN}",
      "toolSchema": {
        "inlinePayload": [
          {
            "name": "mock_backend_admin",
            "description": "Out-of-scope mock backend operation used only to demonstrate that an agent invocation outside the session tool scope is denied.",
            "inputSchema": {
              "type": "object",
              "properties": {
                "session_token": { "type": "string" }
              },
              "required": ["session_token"]
            }
          }
        ]
      }
    }
  }
}
JSON
)"

    mtid="$(aws bedrock-agentcore-control create-gateway-target \
      --region "$REGION" \
      --gateway-identifier "$GW_ID" \
      --name "mock-backend" \
      --target-configuration "$mock_config" \
      --credential-provider-configurations '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]' \
      --query 'targetId' --output text)"
    echo "$mtid" > "$MOCK_TGT_ID_FILE"
    echo "Registered out-of-scope mock-backend target ${mtid} on gateway ${GW_ID}."
    ;;
  destroy)
    for f in "$TGT_ID_FILE" "$MOCK_TGT_ID_FILE"; do
      if [[ -f "$f" ]]; then tid="$(cat "$f")"; else tid=""; fi
      if [[ -n "$tid" ]]; then
        aws bedrock-agentcore-control delete-gateway-target \
          --region "$REGION" --gateway-identifier "$GW_ID" --target-id "$tid" || true
        echo "Deleted target ${tid}."
      fi
      rm -f "$f"
    done
    ;;
  *)
    echo "unknown action: $ACTION" >&2; exit 2 ;;
esac
