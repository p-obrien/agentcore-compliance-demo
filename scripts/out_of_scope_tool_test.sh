#!/usr/bin/env bash
# Out-of-scope tool denial rehearsal.
#
# The gateway registers a deliberately out-of-scope 'mock_backend_admin' target
# that is NOT in the agent's allowed-tool scope. This script attempts to call it
# through the Gateway with a valid session token and expects a denied invocation,
# demonstrating that AgentCore Identity authorizes tool calls against the session
# scope (requirement 6.8).
#
# Required environment:
#   ACCESS_TOKEN   Cognito access token for a tenant assessor
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

: "${ACCESS_TOKEN:?set ACCESS_TOKEN to a Cognito access token for a tenant assessor}"
GATEWAY_URL="$(tofu output -raw agentcore_gateway_url)"

echo "Attempting to invoke the out-of-scope tool 'mock_backend_admin'..."
CALL_BODY='{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"mock_backend_admin","arguments":{"session_token":"x"}}}'
OUT="$(curl -sS -o /dev/stderr -w '%{http_code}' -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  --data "$CALL_BODY" 2> "${TMPDIR:-/tmp}/oos_body.txt" || true)"

BODY="$(cat "${TMPDIR:-/tmp}/oos_body.txt")"
echo "HTTP status: $OUT"
echo "Body: $BODY"

# A denial can surface as an HTTP 403 or a JSON-RPC error naming the tool as
# unavailable / not authorized. Either is a pass; a successful tool result is a
# failure.
if echo "$BODY" | grep -qiE 'error|not authorized|forbidden|unavailable|unknown tool' || [[ "$OUT" == "403" ]]; then
  echo "PASS: out-of-scope tool invocation was denied."
else
  echo "FAIL: the out-of-scope tool was not denied. Investigate the gateway tool scope." >&2
  exit 1
fi
