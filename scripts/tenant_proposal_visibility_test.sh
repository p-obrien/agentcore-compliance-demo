#!/usr/bin/env bash
# Tenant-scoped proposal visibility rehearsal.
#
# Confirms the Approval API lists only proposals for the approver's tenant and
# denies a cross-tenant proposal decision without returning content.
#
# Required environment (two single-tenant approver tokens):
#   APPROVER_A_TOKEN   access token for a user in agency-a-approvers only
#   APPROVER_B_TOKEN   access token for a user in agency-b-approvers only
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

: "${APPROVER_A_TOKEN:?set APPROVER_A_TOKEN to an agency-a-only approver token}"
: "${APPROVER_B_TOKEN:?set APPROVER_B_TOKEN to an agency-b-only approver token}"

API_BASE="$(tofu output -raw approval_api_base_url)"

echo "1) Agency A approver lists pending; expect only agency-a proposals"
curl -sS "$API_BASE/pending" -H "Authorization: Bearer $APPROVER_A_TOKEN" | uv run --no-project python -c '
import json,sys
p=json.load(sys.stdin).get("pending",[])
tenants={x.get("tenant_id") for x in p}
print("tenants seen:", tenants)
print("PASS" if tenants <= {"agency-a"} else "FAIL: cross-tenant proposal leaked")
'

echo
cat <<EOF
2) Agency B approver attempts to decide an agency-a proposal (replace <A_APPROVAL_ID>):
   curl -sS -X POST $API_BASE/decide -H 'Authorization: Bearer \$APPROVER_B_TOKEN' \\
     -H 'content-type: application/json' --data '{"approval_id":"<A_APPROVAL_ID>","decision":"approve"}'
   Expect HTTP 403 and no proposal content in the response body.
EOF

