#!/usr/bin/env bash
# Approval lifecycle rehearsal.
#
# Walks the produced-data lifecycle: a pending proposal exists in staging with
# no record in the mock record store, an authenticated approver commits it, and a
# repeated decision on the now-terminal proposal returns the stored status
# without resuming the task token twice.
#
# Required environment:
#   APPROVER_TOKEN   Cognito access token for a user in a <tenant>-approvers group
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

REGION="${REGION:-ap-southeast-2}"
: "${APPROVER_TOKEN:?set APPROVER_TOKEN to a Cognito access token for an approver}"

API_BASE="$(tofu output -raw approval_api_base_url)"
RECORDS_TABLE="$(tofu output -raw records_table_name)"

echo "=============================================================="
echo "1) List pending proposals for the approver's tenant"
curl -sS "$API_BASE/pending" -H "Authorization: Bearer $APPROVER_TOKEN" | uv run --no-project python -m json.tool || true

echo
echo "2) Confirm no record exists for a PENDING_APPROVAL proposal:"
echo "   aws dynamodb scan --region $REGION --table-name $RECORDS_TABLE --select COUNT"
echo "   (expect Count = 0 before any approval)"

echo
echo "3) Approve one proposal (replace <APPROVAL_ID>):"
echo "   curl -sS -X POST $API_BASE/decide -H 'Authorization: Bearer \$APPROVER_TOKEN' \\\"
echo "     -H 'content-type: application/json' \\\"
echo "     --data '{\"approval_id\":\"<APPROVAL_ID>\",\"decision\":\"approve\"}'"
echo "   Expect the proposal to become APPROVED and exactly one record to appear."

echo
echo "4) Repeat the same decision. Expect a response with idempotent:true and the"
echo "   stored status, and NO second record and NO second task-token resume."
