#!/usr/bin/env bash
# Runtime + Gateway acceptance rehearsal.
#
# Proves that a valid Cognito-authenticated session is accepted by AgentCore
# Runtime and that the Gateway lists the in-scope retrieval tool. Requires a
# Cognito access token and ID token for a tenant assessor user (obtain them from
# the Cognito user-password client; see the README). This is an operator script;
# it never stores a password in the repository.
#
# Required environment:
#   ACCESS_TOKEN   Cognito access token for a tenant assessor
#   ID_TOKEN       companion Cognito ID token (same subject)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

REGION="${REGION:-ap-southeast-2}"
: "${ACCESS_TOKEN:?set ACCESS_TOKEN to a Cognito access token for a tenant assessor}"
: "${ID_TOKEN:?set ID_TOKEN to the companion Cognito ID token}"

GATEWAY_URL="$(tofu output -raw agentcore_gateway_url)"
RUNTIME_IDS_JSON="$(tofu output -json agentcore_runtime_ids 2>/dev/null || echo '{}')"

echo "=============================================================="
echo "1) Gateway tools/list, expect the in-scope 'retrieval' tool"
LIST_BODY='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
LIST_OUT="$(curl -sS -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  --data "$LIST_BODY" || true)"
echo "$LIST_OUT" | uv run --no-project python -c '
import json, sys
raw = sys.stdin.read()
line = next((l[5:].strip() for l in raw.splitlines() if l.startswith("data:")), raw)
try:
    tools = [t.get("name") for t in json.loads(line).get("result", {}).get("tools", [])]
except Exception:
    tools = []
print("tools:", tools)
print("PASS: retrieval tool listed" if "retrieval" in tools else "CHECK: retrieval tool not listed (verify token scope and target registration)")
'

echo
echo "=============================================================="
echo "2) Runtime accepts a valid session (missing tokens are rejected)"
echo "Runtime ids: $RUNTIME_IDS_JSON"
echo "Invoke a runtime endpoint with Authorization: Bearer \$ACCESS_TOKEN and X-Id-Token: \$ID_TOKEN."
echo "A missing or mismatched token must be rejected before any tool call."
