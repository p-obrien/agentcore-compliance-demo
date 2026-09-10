#!/usr/bin/env bash
# Read non-secret OpenSearch targets from OpenTofu and seed the isolated
# collections with a temporary, assumed IAM write role. Run from the repository
# root after `tofu apply`.
#
#   ./seed/load.sh                 # with Titan embeddings
#   ./seed/load.sh --no-embeddings # keyword/text only
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export AWS_REGION="${AWS_REGION:-ap-southeast-2}"
export SEED_TARGETS_JSON="$(tofu output -json opensearch_seed_targets)"
export SEED_ROLE_ARN="$(tofu output -raw opensearch_seed_role_arn)"

# The AWS identity running this command must have sts:AssumeRole permission on
# SEED_ROLE_ARN. The role itself is provisioned by OpenTofu and has no stored
# password or static key.
uv run --project seed python seed/load_seed.py "$@"
