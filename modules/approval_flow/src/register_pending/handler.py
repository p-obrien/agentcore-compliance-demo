"""Attach the Step Functions task token to one pending proposal.

The agent stages the proposal into the pending table before starting the
workflow (see ``agents/intake/agent.py``). This Lambda attaches the current task
token to that proposal while it is ``PENDING_APPROVAL`` and moves it to
``DECIDING`` is deferred to the Approval API claim; here it only records the
token so the approver decision can resume the workflow.

If the proposal does not yet exist (assessment path, which starts the workflow
directly), it is created with status ``PENDING_APPROVAL``. On a retry the token
is never replaced: the conditional expression only sets a token when one is not
already present, so Step Functions continues waiting on the first token.
"""

import json
import os
import time

import boto3
from botocore.exceptions import ClientError

PENDING_TABLE = os.environ["PENDING_TABLE"]
AUDIT_TABLE = os.environ["AUDIT_TABLE"]
_ddb = boto3.client("dynamodb")

PENDING_APPROVAL = "PENDING_APPROVAL"


def handler(event, _context):
    token = event["taskToken"]
    payload = event.get("payload") or {}
    approval_id = payload.get("interaction_id")
    if not isinstance(approval_id, str) or not approval_id:
        raise ValueError("interaction_id must be a non-empty string")

    tenant_id = payload.get("tenant_id", "NONE")
    permit_id = payload.get("permit_id", "")
    draft = payload.get("draft_assessment", "")
    grounding = payload.get("grounding_score")
    review_reason = payload.get("review_reason", "human_review_required")
    verified_subject = payload.get("verified_subject", "")
    now = int(time.time() * 1000)

    # Attach the task token to an existing PENDING_APPROVAL proposal without ever
    # replacing a token already stored by a prior invocation.
    try:
        _ddb.update_item(
            TableName=PENDING_TABLE,
            Key={"approval_id": {"S": approval_id}},
            UpdateExpression="SET task_token = :t, token_attached_at = :n",
            ConditionExpression=(
                "attribute_exists(approval_id) AND #s = :pending "
                "AND attribute_not_exists(task_token)"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":t": {"S": token},
                ":n": {"N": str(now)},
                ":pending": {"S": PENDING_APPROVAL},
            },
        )
        attached = True
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        attached = False

    if not attached:
        # No agent-created proposal to attach to (assessment path), or a retry.
        # Create it fresh; a key collision means a prior invocation already
        # registered a token, so leave that token untouched.
        item = {
            "approval_id": {"S": approval_id},
            "interaction_id": {"S": approval_id},
            "created_at": {"N": str(now)},
            "tenant_id": {"S": tenant_id},
            "permit_id": {"S": permit_id},
            "draft_assessment": {"S": draft[:8000]},
            "grounding_score": {"S": "" if grounding is None else str(grounding)},
            "review_reason": {"S": review_reason},
            "verified_subject": {"S": verified_subject},
            "task_token": {"S": token},
            "status": {"S": PENDING_APPROVAL},
        }
        try:
            _ddb.put_item(
                TableName=PENDING_TABLE,
                Item=item,
                ConditionExpression="attribute_not_exists(approval_id)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            return {"registered": False, "already_registered": True, "approval_id": approval_id}

    _ddb.put_item(
        TableName=AUDIT_TABLE,
        Item={
            "interaction_id": {"S": approval_id},
            "ts": {"N": str(time.time_ns())},
            "tenant_id": {"S": tenant_id},
            "actor_type": {"S": "system"},
            "actor_id": {"S": "register-pending"},
            "action": {"S": "approval_requested"},
            "outcome": {"S": PENDING_APPROVAL},
            "detail": {
                "S": json.dumps(
                    {
                        "approval_id": approval_id,
                        "permit_id": permit_id,
                        "grounding_score": grounding,
                        "review_reason": review_reason,
                        "verified_subject": verified_subject,
                    }
                )
            },
        },
    )
    return {"registered": True, "approval_id": approval_id, "tenant_id": tenant_id}
