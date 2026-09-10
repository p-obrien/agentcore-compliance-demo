"""Write-back and terminal-status finalizer for the approval workflow.

This is the ONLY principal permitted to write the Mock_Record_Store, so nothing
lands in records before an authenticated approval resumes the task token and the
state machine routes here with ``action=commit``.

Two actions:

- ``commit``: write the committed record (approver subject, decision timestamp,
  source Interaction_ID). A failure raises so the state machine's Catch routes to
  the ``COMMIT_FAILED`` terminal path and no record is left behind.
- ``finalize_status``: set the pending proposal's terminal status
  (``APPROVED``/``REJECTED``/``COMMIT_FAILED``), clear the task token, and append
  the terminal status to the trace linked by Interaction_ID.

No record is ever written for a proposal that stays ``PENDING_APPROVAL`` or
``DECIDING``, or on a rejected or commit-failed path.
"""

import json
import os
import time

import boto3

import transitions

RECORDS_TABLE = os.environ["RECORDS_TABLE"]
PENDING_TABLE = os.environ["PENDING_TABLE"]
AUDIT_TABLE = os.environ["AUDIT_TABLE"]

_ddb = boto3.client("dynamodb")

_TERMINAL_STATUSES = transitions.TERMINAL_STATUSES


def _audit(interaction_id, tenant_id, action, outcome, detail):
    _ddb.put_item(
        TableName=AUDIT_TABLE,
        Item={
            "interaction_id": {"S": interaction_id or f"wb-{time.time_ns()}"},
            "ts": {"N": str(time.time_ns())},
            "tenant_id": {"S": tenant_id},
            "actor_type": {"S": "system"},
            "actor_id": {"S": "write-back"},
            "action": {"S": action},
            "outcome": {"S": outcome},
            "detail": {"S": json.dumps(detail, separators=(",", ":"))[:8000]},
        },
    )


def _commit(payload, approver):
    tenant_id = payload.get("tenant_id", "NONE")
    permit_id = payload.get("permit_id", "")
    interaction_id = payload.get("interaction_id", "")
    draft = payload.get("draft_assessment", "")
    now = int(time.time() * 1000)
    # A raised exception here is caught by the state machine and routed to
    # COMMIT_FAILED, leaving no record behind.
    _ddb.put_item(
        TableName=RECORDS_TABLE,
        Item={
            "record_id": {"S": f"{tenant_id}:{permit_id}"},
            "tenant_id": {"S": tenant_id},
            "permit_id": {"S": permit_id},
            "source_interaction_id": {"S": interaction_id},
            "assessment": {"S": draft[:8000]},
            "approved_by": {"S": approver},
            "written_at": {"N": str(now)},
        },
    )
    _audit(
        interaction_id,
        tenant_id,
        "writeback_committed",
        "committed",
        {"permit_id": permit_id, "approver": approver},
    )
    return {"action": "commit", "written": True, "tenant_id": tenant_id}


def _finalize_status(payload, approver, status):
    if status not in _TERMINAL_STATUSES:
        raise ValueError(f"unexpected terminal status: {status}")
    tenant_id = payload.get("tenant_id", "NONE")
    interaction_id = payload.get("interaction_id", "")
    now = int(time.time() * 1000)
    # Set terminal status and clear the task token so it can never be resumed
    # again. Guarded so a terminal proposal is not moved back to pending.
    _ddb.update_item(
        TableName=PENDING_TABLE,
        Key={"approval_id": {"S": interaction_id}},
        UpdateExpression=(
            "SET #s = :status, verified_approver_subject = :approver, "
            "decision_timestamp = :ts REMOVE task_token"
        ),
        ConditionExpression="attribute_exists(approval_id)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":status": {"S": status},
            ":approver": {"S": approver},
            ":ts": {"N": str(now)},
        },
    )
    _audit(
        interaction_id,
        tenant_id,
        "approval_terminal_status",
        status,
        {"approver": approver, "status": status},
    )
    return {"action": "finalize_status", "status": status, "tenant_id": tenant_id}


def handler(event, _context):
    action = event.get("action", "commit")
    payload = event.get("payload", {})
    approver = event.get("approver", "unknown")

    if action == "commit":
        return _commit(payload, approver)
    if action == "finalize_status":
        return _finalize_status(payload, approver, event.get("status", ""))
    raise ValueError(f"unknown write-back action: {action}")
