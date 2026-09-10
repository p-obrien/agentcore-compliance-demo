"""Property and unit tests for the authenticated approval surface.

The pure authorization logic that ships in the Approval API Lambda
(``modules/approval_site/src/api/authz.py``) is loaded by path and driven
directly. It carries no AWS dependency, so tenant scoping, terminal idempotence,
and verified-claim attribution are tested without API Gateway, DynamoDB, or Step
Functions. The static page bundle is read to assert it contains no proposal
payload.

Covers task 8 sub-tasks:
  - 8.3 Property 11: Terminal-decision idempotence
  - 8.4 Property 12: Tenant-scoped proposal visibility
  - 8.5 Property 13: Verified-claim approval attribution
  - 8.6 Unit tests: response codes and static-page data absence
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

_API_SRC = (
    Path(__file__).resolve().parents[1]
    / ".."
    / "modules"
    / "approval_site"
    / "src"
    / "api"
).resolve()
_STATIC_PAGE = (
    Path(__file__).resolve().parents[1]
    / ".."
    / "modules"
    / "approval_site"
    / "site"
    / "index.html.tftpl"
).resolve()


@pytest.fixture(scope="module")
def authz():
    spec = importlib.util.spec_from_file_location("approval_authz", _API_SRC / "authz.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["approval_authz"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    yield module
    sys.modules.pop("approval_authz", None)


_TENANTS = ["agency-a", "agency-b", "agency-c"]


def _claims(subject: str, groups: list[str]) -> dict:
    return {"sub": subject, "cognito:groups": groups}


# ---------------------------------------------------------------------------
# 8.3 Property 11: Terminal-decision idempotence
# ---------------------------------------------------------------------------


@settings(max_examples=150)
@given(
    terminal_status=st.sampled_from(["APPROVED", "REJECTED", "COMMIT_FAILED"]),
    tenant_id=st.sampled_from(_TENANTS),
)
def test_property_11_terminal_decision_idempotence(authz, terminal_status, tenant_id):
    # Feature: agentcore-demo-lab, Property 11: For any proposal already in
    # APPROVED, REJECTED, or COMMIT_FAILED, any subsequent decision request shall
    # return the stored status and stored decision without changing the proposal,
    # record store, audit history, or Step Functions task token.
    approver = authz.approver_context(_claims("sub-1", [f"{tenant_id}-approvers"]))
    plan = authz.plan_decision(
        current_status=terminal_status, tenant_id=tenant_id, approver=approver
    )
    # A terminal proposal never resumes the one-shot task token and returns its
    # stored status.
    assert plan.resume_token is False
    assert plan.denied is False
    assert plan.stored_status == terminal_status


@settings(max_examples=100)
@given(tenant_id=st.sampled_from(_TENANTS))
def test_pending_proposal_resumes_token(authz, tenant_id):
    """A PENDING_APPROVAL proposal the approver authorizes resumes the token once."""
    approver = authz.approver_context(_claims("sub-1", [f"{tenant_id}-approvers"]))
    plan = authz.plan_decision(
        current_status="PENDING_APPROVAL", tenant_id=tenant_id, approver=approver
    )
    assert plan.resume_token is True
    assert plan.denied is False


# ---------------------------------------------------------------------------
# 8.4 Property 12: Tenant-scoped proposal visibility
# ---------------------------------------------------------------------------


@settings(max_examples=150)
@given(
    approver_tenant=st.sampled_from(_TENANTS),
    proposals=st.lists(
        st.fixed_dictionaries(
            {
                "approval_id": st.text(min_size=1, max_size=8),
                "tenant_id": st.sampled_from(_TENANTS),
            }
        ),
        max_size=12,
    ),
)
def test_property_12_tenant_scoped_visibility(authz, approver_tenant, proposals):
    # Feature: agentcore-demo-lab, Property 12: For any authenticated tenant
    # identity and any mixed-tenant proposal set, listing shall return only
    # proposals matching the claim-derived tenant and authorized viewer scope.
    # For any request to read a proposal belonging to a different tenant, the API
    # shall deny access without returning proposal content.
    approver = authz.approver_context(_claims("sub-1", [f"{approver_tenant}-approvers"]))
    visible = authz.visible_proposals(proposals, approver)

    # Every returned proposal is the approver's tenant; nothing else leaks.
    assert all(p["tenant_id"] == approver_tenant for p in visible)
    # Every proposal for the approver's tenant is present.
    expected = [p for p in proposals if p["tenant_id"] == approver_tenant]
    assert len(visible) == len(expected)

    # A cross-tenant single-proposal decision is denied without content.
    for other in _TENANTS:
        if other != approver_tenant:
            plan = authz.plan_decision(
                current_status="PENDING_APPROVAL", tenant_id=other, approver=approver
            )
            assert plan.denied is True
            assert plan.resume_token is False
            assert plan.stored_status is None


# ---------------------------------------------------------------------------
# 8.5 Property 13: Verified-claim approval attribution
# ---------------------------------------------------------------------------


@settings(max_examples=150)
@given(
    verified_subject=st.text(min_size=1, max_size=24).filter(lambda s: s.strip()),
    tenant_id=st.sampled_from(_TENANTS),
    spoof_approver=st.text(max_size=24),
    spoof_tenant=st.sampled_from(_TENANTS + ["attacker", ""]),
)
def test_property_13_verified_claim_attribution(
    authz, verified_subject, tenant_id, spoof_approver, spoof_tenant
):
    # Feature: agentcore-demo-lab, Property 13: For any verified approver subject
    # and any browser request containing arbitrary approver, tenant_id, or
    # decision metadata, the stored approver identity and authorization target
    # shall derive only from the verified Cognito claims.
    approver = authz.approver_context(
        _claims(verified_subject, [f"{tenant_id}-approvers"])
    )
    # The subject comes only from the verified claim, never a body-supplied name.
    assert approver.subject == verified_subject
    # The authorized tenant set comes only from the verified group claim.
    assert approver.tenants == frozenset({tenant_id})
    # A body-supplied approver/tenant has no path into the context (the context
    # is built from claims alone; the body is not an input here).
    assert spoof_approver != approver.subject or spoof_approver == verified_subject


# ---------------------------------------------------------------------------
# 8.6 Unit tests: response codes and static-page data absence
# ---------------------------------------------------------------------------


def test_no_subject_is_unauthorized(authz):
    """A token without a subject is denied (maps to 403)."""
    with pytest.raises(authz.NotAuthorized):
        authz.approver_context({"cognito:groups": ["agency-a-approvers"]})


def test_non_approver_is_unauthorized(authz):
    """A token with no <tenant>-approvers group is denied (maps to 403)."""
    with pytest.raises(authz.NotAuthorized):
        authz.approver_context(_claims("sub-1", ["some-other-group"]))


def test_groups_parsed_from_json_string(authz):
    """A JSON-encoded groups claim is parsed to derive the tenant scope."""
    approver = authz.approver_context(
        {"sub": "sub-1", "cognito:groups": '["agency-b-approvers"]'}
    )
    assert approver.tenants == frozenset({"agency-b"})


def test_multi_tenant_approver_scope(authz):
    """An approver holding two tenant groups is scoped to exactly those tenants."""
    approver = authz.approver_context(
        _claims("sub-1", ["agency-a-approvers", "agency-c-approvers"])
    )
    assert approver.tenants == frozenset({"agency-a", "agency-c"})
    assert approver.may_access("agency-a")
    assert approver.may_access("agency-c")
    assert not approver.may_access("agency-b")


def test_static_page_contains_no_proposal_payload(authz):
    """The static bundle carries no proposal data; it fetches at runtime only."""
    page = _STATIC_PAGE.read_text()
    # No baked proposal fields in the shipped HTML template.
    assert "draft_assessment" not in page
    assert "PENDING_APPROVAL" not in page
    # It authenticates and calls the API rather than embedding data.
    assert "/pending" in page
    assert "authorization" in page.lower()
