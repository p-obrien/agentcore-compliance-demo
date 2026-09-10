"""Property tests for schema-gated, idempotent proposal staging.

The module under test is ``agents/common/staging.py``. It is pure logic over a
repository abstraction, so these tests drive it with the in-memory
``FakeDynamoDBRepository`` (staging table) from the shared fixtures. No AWS call
runs and the record store is never touched.

Covers task 6 sub-tasks:
  - 6.3 Property 7: Idempotent grounding escalation
  - 6.4 Property 8: Schema-gated proposal staging
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_COMMON = Path(__file__).resolve().parents[1] / "common"


@pytest.fixture(scope="module")
def staging_module():
    spec = importlib.util.spec_from_file_location(
        "staging_under_test", _COMMON / "staging.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["staging_under_test"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    yield module
    sys.modules.pop("staging_under_test", None)


def _valid_extraction(permit_id: str = "A-1001") -> dict[str, Any]:
    return {
        "permit_id": permit_id,
        "permit_type": "building",
        "applicant": "Acme Pty Ltd",
        "status": "submitted",
        "summary": "A synthetic permit application for demonstration.",
    }


def _reset(repositories: dict[str, Any]) -> None:
    """Clear every in-memory repository for one hypothesis example.

    Hypothesis runs many examples inside one test-function call over a single
    function-scoped fixture, so the repositories would otherwise carry proposals
    across examples and break "exactly one" assertions.
    """
    for repo in repositories.values():
        repo.items.clear()


# ---------------------------------------------------------------------------
# 6.3 Property 7: Idempotent grounding escalation
# ---------------------------------------------------------------------------


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    tenant_id=st.sampled_from(["agency-a", "agency-b", "agency-c"]),
    repeats=st.integers(min_value=1, max_value=6),
    # Low or absent grounding: a float below threshold, or None.
    grounding_score=st.one_of(st.none(), st.floats(min_value=0.0, max_value=0.4)),
)
def test_property_7_idempotent_grounding_escalation(
    staging_module, dynamodb_repositories, tenant_id, repeats, grounding_score
):
    # Feature: agentcore-demo-lab, Property 7: For any Interaction_ID with a low
    # or absent grounding score, repeated evaluation shall leave exactly one
    # Pending_Proposal in PENDING_APPROVAL with the initial task token
    # unchanged. While that status persists, the workflow shall not invoke the
    # Mock_Record_Store writer.
    staging = dynamodb_repositories["staging"]
    records = dynamodb_repositories["records"]
    interaction_id = "int-grounding"
    _reset(dynamodb_repositories)

    results = [
        staging_module.stage_proposal(
            repository=staging,
            interaction_id=interaction_id,
            tenant_id=tenant_id,
            extraction=_valid_extraction(),
            subject="user-1",
            review_reason="grounding_score_unavailable"
            if grounding_score is None
            else "grounding_below_guardrail_threshold",
            grounding_score=grounding_score,
        )
        for _ in range(repeats)
    ]

    # Exactly one proposal exists, in PENDING_APPROVAL, for this interaction.
    pending = staging.query(interaction_id=interaction_id)
    assert len(pending) == 1
    assert pending[0]["status"] == staging_module.PENDING_APPROVAL
    # Only the first attempt created it; every repeat was a no-op.
    assert results[0].created is True
    assert all(r.created is False for r in results[1:])
    # The record store is never written while the proposal is pending.
    assert records.items == {}


# ---------------------------------------------------------------------------
# 6.4 Property 8: Schema-gated proposal staging
# ---------------------------------------------------------------------------


_REQUIRED = ("permit_id", "permit_type", "applicant", "status", "summary")


@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    tenant_id=st.sampled_from(["agency-a", "agency-b", "agency-c"]),
    # Drop one required field, add an unexpected field, or wrong-type a field.
    breakage=st.sampled_from(["drop_field", "extra_field", "wrong_type", "not_object"]),
    drop=st.sampled_from(_REQUIRED),
)
def test_property_8_schema_invalid_creates_nothing(
    staging_module, dynamodb_repositories, tenant_id, breakage, drop
):
    # Feature: agentcore-demo-lab, Property 8 (invalid half): For any
    # schema-invalid extraction, staging shall create neither a Pending_Proposal
    # nor a Mock_Record_Store record.
    staging = dynamodb_repositories["staging"]
    records = dynamodb_repositories["records"]
    _reset(dynamodb_repositories)

    extraction: Any = _valid_extraction()
    if breakage == "drop_field":
        extraction.pop(drop)
    elif breakage == "extra_field":
        extraction["injected"] = "unexpected"
    elif breakage == "wrong_type":
        extraction["permit_id"] = 12345  # not a string
    else:  # not_object
        extraction = ["not", "an", "object"]

    with pytest.raises(staging_module.SchemaInvalid):
        staging_module.stage_proposal(
            repository=staging,
            interaction_id="int-invalid",
            tenant_id=tenant_id,
            extraction=extraction,
            subject="user-1",
            review_reason="grounding_verified",
            grounding_score=0.9,
        )

    assert staging.items == {}
    assert records.items == {}


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    tenant_id=st.sampled_from(["agency-a", "agency-b", "agency-c"]),
    permit_id=st.from_regex(r"[A-C]-[0-9]{4}", fullmatch=True),
)
def test_property_8_schema_valid_creates_one_proposal(
    staging_module, dynamodb_repositories, tenant_id, permit_id
):
    # Feature: agentcore-demo-lab, Property 8 (valid half): For any schema-valid
    # agent extraction and Tenant_Context, staging shall create exactly one
    # proposal containing the context tenant and Interaction_ID.
    staging = dynamodb_repositories["staging"]
    interaction_id = "int-valid"
    _reset(dynamodb_repositories)

    result = staging_module.stage_proposal(
        repository=staging,
        interaction_id=interaction_id,
        tenant_id=tenant_id,
        extraction=_valid_extraction(permit_id),
        subject="user-1",
        review_reason="grounding_verified",
        grounding_score=0.95,
    )

    assert result.created is True
    stored = staging.query(interaction_id=interaction_id)
    assert len(stored) == 1
    assert stored[0]["tenant_id"] == tenant_id
    assert stored[0]["interaction_id"] == interaction_id
    assert stored[0]["status"] == staging_module.PENDING_APPROVAL
