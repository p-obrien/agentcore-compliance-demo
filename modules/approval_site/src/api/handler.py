"""Authenticated, tenant-scoped, attributable approval API.

Approver identity and the tenants an approver may act on are derived only from
verified Cognito claims (see ``authz.py``); the request body never supplies an
approver or a tenant. Listing is scoped to the approver's tenants via the
``by_tenant_status`` GSI. A decision on a terminal proposal returns the stored
status without resuming the one-shot Step Functions task token.
"""

import json
import os
import time

import boto3
from botocore.exceptions import ClientError

import authz

PENDING_TABLE = os.environ["PENDING_TABLE"]
AUDIT_TABLE = os.environ["AUDIT_TABLE"]
_ddb = boto3.client("dynamodb")
_sfn = boto3.client("stepfunctions")

PENDING_APPROVAL = authz.PENDING_APPROVAL
DECIDING = authz.DECIDING

CORS = {
    "Access-Control-Allow-Origin": os.environ["APPROVAL_ORIGIN"],
    "Access-Control-Allow-Headers": "authorization,content-type",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Content-Type": "application/json",
}


def _resp(status, body):
    return {"statusCode": status, "headers": CORS, "body": json.dumps(body)}


def _claims(event):
    return (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )


def _audit(interaction_id, tenant_id, action, outcome, detail):
    _ddb.put_item(
        TableName=AUDIT_TABLE,
        Item={
            "interaction_id": {"S": interaction_id},
            "ts": {"N": str(time.time_ns())},
            "tenant_id": {"S": tenant_id},
            "actor_type": {"S": "approver"},
            "actor_id": {"S": detail.get("approver_subject", "unknown")},
            "action": {"S": action},
            "outcome": {"S": outcome},
            "detail": {"S": json.dumps(detail)[:8000]},
        },
    )


def _list_pending(approver: "authz.ApproverContext"):
    """List PENDING_APPROVAL proposals for each tenant the approver authorizes.

    Uses the by_tenant_status GSI with the verified tenant as the hash key, so a
    proposal for an unauthorized tenant is never read.
    """
    proposals = []
    for tenant_id in sorted(approver.tenants):
        result = _ddb.query(
            TableName=PENDING_TABLE,
            IndexName="by_tenant_status",
            KeyConditionExpression="tenant_id = :t AND #s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":t": {"S": tenant_id},
                ":pending": {"S": PENDING_APPROVAL},
            },
        )
        for item in result.get("Items", []):
            proposals.append(
                {
                    "approval_id": item["approval_id"]["S"],
                    "tenant_id": item["tenant_id"]["S"],
                    "permit_id": item.get("permit_id", {}).get("S", ""),
                    "review_reason": item.get("review_reason", {}).get("S", ""),
                    "grounding_score": item.get("grounding_score", {}).get("S", ""),
                    "created_at": item.get("created_at", {}).get("N", ""),
                }
            )
    return proposals


def _get_proposal(approval_id):
    response = _ddb.get_item(TableName=PENDING_TABLE, Key={"approval_id": {"S": approval_id}})
    return response.get("Item")


def _claim_pending(approval_id, approver_subject, decision):
    """Atomically move PENDING_APPROVAL -> DECIDING, or fail if not pending."""
    try:
        response = _ddb.update_item(
            TableName=PENDING_TABLE,
            Key={"approval_id": {"S": approval_id}},
            ConditionExpression="#status = :pending",
            UpdateExpression="SET #status = :deciding, decided_by = :approver, decision = :decision",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":pending": {"S": PENDING_APPROVAL},
                ":deciding": {"S": DECIDING},
                ":approver": {"S": approver_subject},
                ":decision": {"S": decision},
            },
            ReturnValues="ALL_OLD",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ValueError("approval is not pending") from exc
        raise
    return response["Attributes"]


def _restore_pending(approval_id):
    _ddb.update_item(
        TableName=PENDING_TABLE,
        Key={"approval_id": {"S": approval_id}},
        ConditionExpression="#status = :deciding",
        UpdateExpression="SET #status = :pending REMOVE decided_by, decision",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":deciding": {"S": DECIDING}, ":pending": {"S": PENDING_APPROVAL}},
    )


def _decide(body, approver: "authz.ApproverContext"):
    approval_id = body.get("approval_id")
    decision = body.get("decision")
    if not isinstance(approval_id, str) or decision not in {"approve", "reject"}:
        raise ValueError("approval_id and decision (approve|reject) are required")

    item = _get_proposal(approval_id)
    if item is None:
        raise ValueError("approval not found")
    tenant_id = item["tenant_id"]["S"]
    interaction_id = item["interaction_id"]["S"]
    current_status = item["status"]["S"]

    plan = authz.plan_decision(
        current_status=current_status, tenant_id=tenant_id, approver=approver
    )
    if plan.denied:
        # Cross-tenant: deny without returning proposal content.
        raise PermissionError("approver is not authorized for this tenant")
    if not plan.resume_token:
        # Terminal (or DECIDING): return the stored status without resuming the
        # one-shot task token or changing the proposal.
        return {
            "ok": True,
            "approval_id": approval_id,
            "status": plan.stored_status or current_status,
            "decision": item.get("decision", {}).get("S", ""),
            "idempotent": True,
        }

    claimed = _claim_pending(approval_id, approver.subject, decision)
    try:
        _audit(
            interaction_id,
            tenant_id,
            "approval_decision_authorized",
            decision,
            {"approval_id": approval_id, "decision": decision, "approver_subject": approver.subject},
        )
        _sfn.send_task_success(
            taskToken=claimed["task_token"]["S"],
            output=json.dumps(
                {"decision": decision, "approver": approver.subject, "approval_id": approval_id},
                separators=(",", ":"),
            ),
        )
    except Exception:
        _restore_pending(approval_id)
        raise

    return {"ok": True, "approval_id": approval_id, "decision": decision}


def handler(event, _context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")
    if method == "OPTIONS":
        return _resp(204, {})
    try:
        approver = authz.approver_context(_claims(event))
        if method == "GET" and path.endswith("/pending"):
            return _resp(200, {"pending": _list_pending(approver)})
        if method == "POST" and path.endswith("/decide"):
            return _resp(200, _decide(json.loads(event.get("body") or "{}"), approver))
        return _resp(404, {"error": "not found"})
    except authz.NotAuthorized as exc:
        return _resp(403, {"error": str(exc)})
    except PermissionError as exc:
        return _resp(403, {"error": str(exc)})
    except ValueError as exc:
        return _resp(409, {"error": str(exc)})
    except Exception as exc:
        print(f"approval api failure: {exc}")
        return _resp(500, {"error": "approval action failed"})
