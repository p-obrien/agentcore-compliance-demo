"""Property test for teardown remediation completeness (Property 18).

The teardown verifier is a shell script, so this test analyzes its text: every
resource type it checks must supply a non-empty manual-removal command, the
checked set must cover the resource types the teardown procedure lists, and the
script must exit non-zero when any resource remains. No AWS call is made.

Covers task 14 sub-task:
  - 14.5 Property 18: Teardown remediation completeness
"""

from __future__ import annotations

import re
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[1] / ".." / "scripts" / "verify_teardown.sh"
).resolve()

# The resource types the teardown procedure must verify (Requirement 13.3/13.5).
_REQUIRED_TYPES = {
    "opensearch_domains",
    "agentcore_gateways",
    "agentcore_runtimes",
    "dynamodb_tables",
    "step_functions",
    "lambda_functions",
    "cloudfront_distributions",
    "ecr_repositories",
    "s3_buckets",
    "vpc_endpoints",
    "security_groups",
    "iam_roles",
}


def _report_calls(text: str):
    """Extract (resource_type, remediation_command) pairs from report() calls.

    Matches: report "<kind>" "$COUNT" "<command>"
    """
    pairs = {}
    for match in re.finditer(r'report\s+"([^"]+)"\s+"\$\{?[A-Za-z0-9_]+\}?"\s+"([^"]*)"', text):
        pairs[match.group(1)] = match.group(2)
    return pairs


def test_property_18_every_checked_type_has_remediation_command():
    # Feature: agentcore-demo-lab, Property 18: For teardown, every resource type
    # the verifier reports as remaining shall carry a manual-removal or retry
    # command, and the verifier shall exit non-zero when any resource remains.
    text = _SCRIPT.read_text()
    pairs = _report_calls(text)
    assert pairs, "expected report() calls in verify_teardown.sh"
    for kind, command in pairs.items():
        assert command.strip(), f"resource type {kind} has no remediation command"
        # A remediation command must be an actionable CLI invocation.
        assert "aws " in command, f"resource type {kind} command is not an aws CLI command: {command!r}"


def test_property_18_covers_required_resource_types():
    """The verifier checks every resource type the teardown procedure lists."""
    text = _SCRIPT.read_text()
    checked = set(_report_calls(text))
    missing = _REQUIRED_TYPES - checked
    assert not missing, f"teardown verifier does not check: {sorted(missing)}"


def test_property_18_exits_nonzero_when_resources_remain():
    """The script sets a failure state and exits non-zero when resources remain."""
    text = _SCRIPT.read_text()
    # The remaining-resource path sets REMAINING=1 and exits 1.
    assert "REMAINING=1" in text
    assert re.search(r'REMAINING"?\s*-eq\s*0', text), "expected a REMAINING==0 success gate"
    assert "exit 1" in text, "expected a non-zero exit on the remaining-resource path"
