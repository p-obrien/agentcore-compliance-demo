#!/usr/bin/env bash
# Dedicated-tier access-policy denial rehearsal.
#
# Proves the dedicated domain's resource-based access policy denies the SHARED
# retrieval principal, independently of any query filter. We assume the shared
# read role and SigV4-sign a search directly against the Agency C dedicated
# domain endpoint. The expected result is an authorization-denied response
# (HTTP 403) and NO document payload, distinct from a zero-document query.
#
# This is an operator-only rehearsal; it does not represent application traffic.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

REGION="${REGION:-ap-southeast-2}"
DEDICATED_ENDPOINT="$(tofu output -json opensearch_managed_domain_endpoints | uv run --no-project python -c 'import json,sys; print(json.load(sys.stdin)["dedicated"])')"
SHARED_ROLE_ARN="$(tofu output -raw opensearch_shared_retrieval_role_arn)"

if [[ -z "$SHARED_ROLE_ARN" || -z "$DEDICATED_ENDPOINT" ]]; then
  echo "Missing opensearch_shared_retrieval_role_arn or dedicated endpoint output; run tofu apply first." >&2
  exit 2
fi

echo "Assuming the SHARED retrieval role and targeting the DEDICATED (Agency C) domain."
CREDS_JSON="$(aws sts assume-role \
  --region "$REGION" \
  --role-arn "$SHARED_ROLE_ARN" \
  --role-session-name dedicated-denial-rehearsal \
  --query Credentials --output json)"

export AWS_ACCESS_KEY_ID="$(printf '%s' "$CREDS_JSON" | uv run --no-project python -c 'import json,sys;print(json.load(sys.stdin)["AccessKeyId"])')"
export AWS_SECRET_ACCESS_KEY="$(printf '%s' "$CREDS_JSON" | uv run --no-project python -c 'import json,sys;print(json.load(sys.stdin)["SecretAccessKey"])')"
export AWS_SESSION_TOKEN="$(printf '%s' "$CREDS_JSON" | uv run --no-project python -c 'import json,sys;print(json.load(sys.stdin)["SessionToken"])')"

# SigV4-sign a search against the dedicated domain with the shared principal.
STATUS="$(uv run --no-project python - "$DEDICATED_ENDPOINT" "$REGION" <<'PY'
import sys, json, urllib.request, urllib.error
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

endpoint, region = sys.argv[1], sys.argv[2]
url = endpoint.rstrip("/") + "/permits/_search"
body = json.dumps({"query": {"match_all": {}}, "size": 1}).encode()
req = AWSRequest(method="POST", url=url, data=body, headers={"Content-Type": "application/json"})
SigV4Auth(boto3.Session().get_credentials(), "es", region).add_auth(req)
try:
    with urllib.request.urlopen(urllib.request.Request(url, data=body, method="POST", headers=dict(req.headers.items())), timeout=15) as r:
        print(f"UNEXPECTED_ALLOW:{r.status}")
except urllib.error.HTTPError as e:
    print(f"{e.code}")
except Exception as e:
    print(f"ERROR:{type(e).__name__}")
PY
)"

echo "Dedicated-domain response status for the shared principal: $STATUS"
case "$STATUS" in
  401|403)
    echo "PASS: dedicated access policy denied the shared principal, no document payload returned." ;;
  UNEXPECTED_ALLOW:*)
    echo "FAIL: the shared principal was ALLOWED to read the dedicated domain. Investigate the deny statement." >&2
    exit 1 ;;
  *)
    echo "INCONCLUSIVE: unexpected response ($STATUS). Re-run after domain initialization completes." >&2
    exit 1 ;;
esac

echo
echo "Expected audit evidence (written by the application retrieval path, not this direct probe):"
echo "  event_type dedicated_domain_access_denied with the requesting principal and Interaction_ID."
