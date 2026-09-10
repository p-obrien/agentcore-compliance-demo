#!/usr/bin/env bash
# Trace reconstruction rehearsal.
#
# Invokes the read-only trace-read Lambda for one Interaction_ID and prints the
# projection: tenant, ordered events, models, MCP calls, guardrail results,
# proposal, and approval outcome. The projection excludes task tokens, bearer
# tokens, and secrets.
#
# Usage: scripts/verify_trace.sh <INTERACTION_ID>
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

REGION="${REGION:-ap-southeast-2}"
INTERACTION_ID="${1:?usage: verify_trace.sh <INTERACTION_ID>}"
FN="$(tofu output -raw trace_read_function_name)"
OUT="${TMPDIR:-/tmp}/trace_${INTERACTION_ID//[^A-Za-z0-9_-]/_}.json"

aws lambda invoke \
  --region "$REGION" \
  --function-name "$FN" \
  --cli-binary-format raw-in-base64-out \
  --payload "{\"interaction_id\":\"$INTERACTION_ID\"}" \
  "$OUT" >/dev/null

uv run --no-project python - "$OUT" <<'PY'
import json, sys
resp = json.load(open(sys.argv[1]))
body = json.loads(resp["body"]) if "body" in resp else resp
for key in ("interaction_id", "tenant_id", "model_ids", "mcp_calls", "guardrail_results", "proposal", "approval_outcome"):
    print(f"{key}: {body.get(key)}")
blob = json.dumps(body).lower()
leaked = [m for m in ("task_token", "bearer ", "secret", "password") if m in blob]
print("PASS: no secrets in trace" if not leaked else f"FAIL: possible secret markers {leaked}")
print(f"events: {len(body.get('events', []))}")
PY
