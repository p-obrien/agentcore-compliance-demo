"""Unit tests for required README runbook disclosures.

The README ships with the lab and must carry the control-ownership, tier
enforcement, synthetic-data, and non-production/non-IRAP disclosures, plus the
documented seed retry schedule. This test reads the file and asserts each
required statement is present. No AWS call is made.

Covers task 15 sub-task:
  - 15.2 Unit tests for runbook disclosures
"""

from __future__ import annotations

from pathlib import Path

_README = (Path(__file__).resolve().parents[2] / "README.md").resolve()


def _text() -> str:
    return _README.read_text().lower()


def test_readme_states_control_ownership():
    """Intelligence_API/OpenSearch own filtering; AgentCore owns identity/scope."""
    text = _text()
    assert "opensearch" in text
    assert "retrieval filtering" in text
    assert "agentcore" in text and "session isolation" in text
    # AgentCore explicitly does not replace the retrieval filtering / access policies.
    assert "does not replace" in text


def test_readme_states_tier_enforcement():
    """Shared tier is filter-enforced; dedicated tier is access-policy-enforced."""
    text = _text()
    assert "filter-enforced" in text
    assert "access-policy-enforced" in text


def test_readme_states_synthetic_data_only():
    text = _text()
    assert "synthetic" in text
    assert "does not use any production data" in text


def test_readme_states_non_production_and_non_irap():
    text = _text()
    assert "irap" in text
    assert "does not represent a production readiness determination" in text


def test_readme_documents_seed_retry_schedule():
    """The README documents the retryable statuses, max retries, and delay schedule."""
    text = _text()
    assert "403" in text and "429" in text and "503" in text
    assert "5 retries" in text or "maximum retries: **5**" in text or "**5**" in text
    assert "exponential backoff" in text
    assert "0.5" in text and "8 seconds" in text


def test_readme_documents_teardown_and_confirmation():
    """The README gives one teardown command and a post-teardown confirmation step."""
    text = _text()
    assert "tofu destroy" in text
    assert "verify_teardown.sh" in text


def test_readme_describes_three_tenants_and_two_domains():
    text = _text()
    assert "agency a" in text and "agency b" in text and "agency c" in text
    assert "two" in text and "managed domain" in text
