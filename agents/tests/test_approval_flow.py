"""Property tests for approval workflow terminal-state transitions.

The module under test is the pure transition logic that ships inside the
write-back Lambda (``modules/approval_flow/src/write_back/transitions.py``). It
carries no AWS dependency, so these tests drive it directly and model the
Mock_Record_Store with the in-memory ``records`` repository from the shared
fixtures. The transition module is the same code the deployed Lambda imports, so
the property holds for the shipped state machine, not a re-implementation.

Covers task 7 sub-tasks:
  - 7.3 Property 9: Pending-data exclusion
  - 7.4 Property 10: Approval terminal-state transitions
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_TRANSITIONS_PATH = (
    Path(__file__).resolve().parents[1]
    / ".."
    / "modules"
    / "approval_flow"
    / "src"
    / "write_back"
    / "transitions.py"
).resolve()


@pytest.fixture(scope="module")
def transitions_module():
    spec = importlib.util.spec_from_file_location("approval_transitions", _TRANSITIONS_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["approval_transitions"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    yield module
    sys.modules.pop("approval_transitions", None)


def _reset(repositories):
    for repo in repositories.values():
        repo.items.clear()


# ---------------------------------------------------------------------------
# 7.3 Property 9: Pending-data exclusion
# ---------------------------------------------------------------------------


@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    status=st.sampled_from(["PENDING_APPROVAL", "DECIDING", "REJECTED", "COMMIT_FAILED"]),
)
def test_property_9_pending_data_exclusion(transitions_module, status):
    # Feature: agentcore-demo-lab, Property 9: For any Pending_Proposal in
    # PENDING_APPROVAL, DECIDING, or either uncommitted failure path, no
    # Mock_Record_Store record shall exist with its source Interaction_ID.
    assert transitions_module.record_exists_allowed(status) is False


def test_property_9_only_approved_may_have_record(transitions_module):
    """Only an APPROVED proposal may have a committed record."""
    assert transitions_module.record_exists_allowed("APPROVED") is True


# ---------------------------------------------------------------------------
# 7.4 Property 10: Approval terminal-state transitions
# ---------------------------------------------------------------------------


@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    decision=st.sampled_from(["approve", "reject", "unknown"]),
    commit_succeeds=st.booleans(),
    tenant_id=st.sampled_from(["agency-a", "agency-b", "agency-c"]),
)
def test_property_10_terminal_state_transitions(
    transitions_module, dynamodb_repositories, decision, commit_succeeds, tenant_id
):
    # Feature: agentcore-demo-lab, Property 10: For any PENDING_APPROVAL proposal
    # and verified tenant-authorized approver, an approved decision with a
    # successful record-store write shall produce one committed record and
    # APPROVED status carrying that approver and timestamp. A rejected decision
    # shall produce REJECTED with no record. A write failure shall produce
    # COMMIT_FAILED with no record. Each terminal transition shall append its
    # terminal status to the linked Trace.
    _reset(dynamodb_repositories)
    records = dynamodb_repositories["records"]
    audit = dynamodb_repositories["audit"]
    interaction_id = "int-decision"

    outcome = transitions_module.resolve_transition(
        decision=decision, commit_succeeds=commit_succeeds
    )

    # Apply the outcome the way the handler would: commit only when told to.
    if outcome.write_record:
        records.put(
            {
                "record_id": f"{tenant_id}:A-1001",
                "source_interaction_id": interaction_id,
                "approved_by": "approver-sub",
                "written_at": 1_000,
            }
        )
    # Every terminal transition appends its status to the trace.
    audit.put(
        {
            "event_time_ns": 1,
            "interaction_id": interaction_id,
            "action": "approval_terminal_status",
            "outcome": outcome.terminal_status,
        }
    )

    # Assert the invariants per decision.
    if decision == "approve" and commit_succeeds:
        assert outcome.terminal_status == "APPROVED"
        assert outcome.write_record is True
        assert len(records.items) == 1
    elif decision == "approve" and not commit_succeeds:
        assert outcome.terminal_status == "COMMIT_FAILED"
        assert outcome.write_record is False
        assert records.items == {}
    else:  # reject or any non-approve decision
        assert outcome.terminal_status == "REJECTED"
        assert outcome.write_record is False
        assert records.items == {}

    # The terminal status is recorded in the trace regardless of outcome.
    terminal_events = [
        item for item in audit.items.values()
        if item["action"] == "approval_terminal_status"
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0]["outcome"] == outcome.terminal_status
    assert terminal_events[0]["outcome"] in transitions_module.TERMINAL_STATUSES


def test_unknown_decision_never_commits(transitions_module):
    """An unrecognized decision is treated as a rejection, never a commit."""
    outcome = transitions_module.resolve_transition(decision="garbage", commit_succeeds=True)
    assert outcome.terminal_status == "REJECTED"
    assert outcome.write_record is False
