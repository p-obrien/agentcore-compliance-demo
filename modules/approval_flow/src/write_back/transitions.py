"""Pure approval state-transition rules for the write-back Lambda.

These functions carry no AWS dependency so they can be property-tested directly.
The handler applies their decisions against DynamoDB and Step Functions.

Invariants enforced here:
  - A record is produced only on a successful commit (approve + write success).
  - Reject and commit-failure produce no record.
  - Every terminal transition names one of APPROVED / REJECTED / COMMIT_FAILED.
  - A proposal that is still PENDING_APPROVAL or DECIDING has no record.
"""

from __future__ import annotations

from dataclasses import dataclass

PENDING_APPROVAL = "PENDING_APPROVAL"
DECIDING = "DECIDING"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
COMMIT_FAILED = "COMMIT_FAILED"

TERMINAL_STATUSES = frozenset({APPROVED, REJECTED, COMMIT_FAILED})
NON_TERMINAL_STATUSES = frozenset({PENDING_APPROVAL, DECIDING})


@dataclass(frozen=True)
class TransitionOutcome:
    """Result of resolving one decision against a proposal.

    ``write_record`` is ``True`` only for an approved decision whose commit
    succeeds. ``terminal_status`` is the status the proposal moves to.
    """

    terminal_status: str
    write_record: bool


def resolve_transition(*, decision: str, commit_succeeds: bool) -> TransitionOutcome:
    """Resolve the terminal outcome for one authenticated decision.

    ``decision`` is ``approve`` or ``reject`` (any other value is treated as a
    rejection, so an unknown decision never commits a record). ``commit_succeeds``
    models whether the Mock_Record_Store write succeeded on an approval.
    """
    if decision == "approve":
        if commit_succeeds:
            return TransitionOutcome(terminal_status=APPROVED, write_record=True)
        return TransitionOutcome(terminal_status=COMMIT_FAILED, write_record=False)
    return TransitionOutcome(terminal_status=REJECTED, write_record=False)


def record_exists_allowed(status: str) -> bool:
    """Whether a Mock_Record_Store record may exist for a proposal in ``status``.

    Only an ``APPROVED`` proposal may have a committed record. Any non-terminal
    status, a rejection, or a commit failure must have no record.
    """
    return status == APPROVED
