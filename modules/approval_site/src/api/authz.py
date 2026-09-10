"""Pure approval-authorization logic for the Approval API.

The Approval API derives the approver subject and the tenants an approver may
act on solely from verified Cognito claims, never from the request body. These
functions carry no AWS dependency so they can be property-tested directly; the
handler applies their decisions against DynamoDB and Step Functions.

Tenant-scoped approver model: an approver holds one or more
``<tenant>-approvers`` groups (``agency-a-approvers`` ...). The tenant set is
derived from those groups; a proposal for a tenant the approver does not hold is
denied without returning content.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

TENANTS = ("agency-a", "agency-b", "agency-c")
_APPROVER_SUFFIX = "-approvers"

TERMINAL_STATUSES = frozenset({"APPROVED", "REJECTED", "COMMIT_FAILED"})
PENDING_APPROVAL = "PENDING_APPROVAL"
DECIDING = "DECIDING"


class NotAuthorized(PermissionError):
    """Raised when a caller lacks an authenticated identity or approver scope."""


@dataclass(frozen=True)
class ApproverContext:
    """Verified approver identity and the tenants it may act on.

    ``subject`` is the Cognito ``sub``; ``tenants`` is the frozenset of tenant
    identifiers the approver is authorized for, derived only from verified
    ``<tenant>-approvers`` group claims.
    """

    subject: str
    tenants: frozenset[str]

    def may_access(self, tenant_id: str) -> bool:
        return tenant_id in self.tenants


def parse_groups(raw: object) -> list[str]:
    """Normalize a ``cognito:groups`` claim (list or delimited string) to a list."""
    if isinstance(raw, list):
        return [g for g in raw if isinstance(g, str)]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw) if raw.startswith("[") else raw.split(",")
        except json.JSONDecodeError:
            return []
        return [g.strip() for g in parsed if isinstance(g, str) and g.strip()]
    return []


def approver_context(claims: dict) -> ApproverContext:
    """Derive the verified approver context from Cognito claims.

    Raises ``NotAuthorized`` when the token has no subject or holds no
    ``<tenant>-approvers`` group for a known tenant. Nothing is read from the
    request body.
    """
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise NotAuthorized("authenticated token has no subject")
    groups = parse_groups(claims.get("cognito:groups"))
    tenants = {
        group[: -len(_APPROVER_SUFFIX)]
        for group in groups
        if group.endswith(_APPROVER_SUFFIX) and group[: -len(_APPROVER_SUFFIX)] in TENANTS
    }
    if not tenants:
        raise NotAuthorized("authenticated user is not an approver for any tenant")
    return ApproverContext(subject=subject, tenants=frozenset(tenants))


@dataclass(frozen=True)
class DecisionPlan:
    """What a decision request should do given the proposal's current status.

    ``resume_token`` is ``True`` only for a proposal still ``PENDING_APPROVAL``
    that the approver may act on. A terminal proposal yields ``resume_token=False``
    and ``stored_status`` set, so the caller returns the stored decision without
    resuming the one-shot task token.
    """

    resume_token: bool
    stored_status: str | None
    denied: bool


def plan_decision(*, current_status: str, tenant_id: str, approver: ApproverContext) -> DecisionPlan:
    """Decide how to handle a decision request against a proposal.

    - Cross-tenant: denied, no content, no token resume.
    - Terminal: return stored status, no token resume (idempotent).
    - Pending and authorized: resume the token.
    """
    if not approver.may_access(tenant_id):
        return DecisionPlan(resume_token=False, stored_status=None, denied=True)
    if current_status in TERMINAL_STATUSES:
        return DecisionPlan(resume_token=False, stored_status=current_status, denied=False)
    if current_status == PENDING_APPROVAL:
        return DecisionPlan(resume_token=True, stored_status=None, denied=False)
    # DECIDING or any other non-pending, non-terminal status: do not resume.
    return DecisionPlan(resume_token=False, stored_status=current_status, denied=False)


def visible_proposals(proposals: list[dict], approver: ApproverContext) -> list[dict]:
    """Filter a proposal list to only those the approver's tenants authorize."""
    return [p for p in proposals if approver.may_access(p.get("tenant_id", ""))]
