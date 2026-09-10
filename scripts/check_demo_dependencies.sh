#!/usr/bin/env bash
# Pre-walkthrough dependency check.
#
# Prints one status line per scripted demonstration action. If a dependency is
# unavailable, it prints the prepared recording path assigned to that action so
# the facilitator can fall back to the recording instead of a live failure.
#
# Recordings live under recordings/<action>.mp4 (create them before the demo).
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

REGION="${REGION:-ap-southeast-2}"
RECORDINGS_DIR="${RECORDINGS_DIR:-recordings}"
FAIL=0

check() {
  local action="$1"; shift
  if "$@" >/dev/null 2>&1; then
    printf 'OK    %s\n' "$action"
  else
    FAIL=1
    printf 'FAIL  %s  -> fallback recording: %s/%s.mp4\n' "$action" "$RECORDINGS_DIR" "$action"
  fi
}

have_output() { tofu output -raw "$1" >/dev/null 2>&1; }
domain_reachable() {
  local ep; ep="$(tofu output -json opensearch_managed_domain_endpoints 2>/dev/null | uv run --no-project python -c "import json,sys;print(json.load(sys.stdin).get('$1',''))" 2>/dev/null)"
  [[ -n "$ep" ]]
}

check "shared_tier_retrieval"      domain_reachable shared
check "dedicated_tier_retrieval"   domain_reachable dedicated
check "cross_tenant_block"         have_output retrieval_tool_function_name
check "tenant_spoof_ignored"       have_output audit_table_name
check "dedicated_domain_denial"    have_output opensearch_shared_retrieval_role_arn
check "guardrail_poisoned_document" have_output agentcore_gateway_url
check "human_approval"             have_output state_machine_arn
check "approval_page"              have_output approval_page_url
check "tenant_proposal_visibility" have_output approval_api_base_url
check "audit_trace"                have_output trace_read_function_name
check "runtime_gateway"            have_output agentcore_gateway_url
check "out_of_scope_tool"          have_output agentcore_gateway_url

echo
if [[ "$FAIL" -eq 0 ]]; then
  echo "All demonstration dependencies are available."
else
  echo "One or more dependencies are unavailable; use the fallback recordings above." >&2
  exit 1
fi
