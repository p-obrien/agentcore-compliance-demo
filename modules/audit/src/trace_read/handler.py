"""Read-only trace projection for one Interaction_ID.

Queries the append-only audit table by ``interaction_id`` and folds the events
into a read-only view: tenant, ordered events, models, MCP calls, guardrail
results, proposal, and approval outcome. Task tokens, bearer tokens, and secrets
are stripped defensively before anything is returned. This Lambda has only
``dynamodb:Query`` on the audit table; it can neither write nor delete.
"""

import json
import os

import boto3

AUDIT_TABLE = os.environ["AUDIT_TABLE"]
_ddb = boto3.client("dynamodb")

_SECRET_MARKERS = (
    "tasktoken",
    "bearertoken",
    "accesstoken",
    "idtoken",
    "secret",
    "password",
    "credential",
    "authorization",
    "sessiontoken",
    "apikey",
)


def _is_secret_key(key):
    normalized = key.replace("_", "").replace("-", "").lower()
    return any(marker in normalized for marker in _SECRET_MARKERS)


def _redact(value):
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items() if not _is_secret_key(str(k))}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _decode(item):
    """Decode a DynamoDB audit item into a plain record."""
    out = {}
    for key, av in item.items():
        if "S" in av:
            out[key] = av["S"]
        elif "N" in av:
            out[key] = int(av["N"])
        else:
            out[key] = next(iter(av.values()))
    detail = out.get("detail")
    if isinstance(detail, str):
        try:
            out["detail"] = json.loads(detail)
        except json.JSONDecodeError:
            out["detail"] = {}
    return out


def _project(records):
    ordered = sorted(records, key=lambda r: r.get("ts", 0))
    projection = {
        "interaction_id": None,
        "tenant_id": None,
        "model_ids": [],
        "mcp_calls": [],
        "guardrail_results": [],
        "proposal": None,
        "approval_outcome": None,
        "events": [],
    }
    for record in ordered:
        action = record.get("action", "")
        projection["interaction_id"] = projection["interaction_id"] or record.get("interaction_id")
        projection["tenant_id"] = projection["tenant_id"] or record.get("tenant_id")
        model_id = record.get("model_id")
        if model_id and model_id not in projection["model_ids"]:
            projection["model_ids"].append(model_id)
        if record.get("actor_type") == "tool" or record.get("tool_id"):
            projection["mcp_calls"].append(
                {"action": action, "tool_id": record.get("tool_id"), "outcome": record.get("outcome")}
            )
        if action.startswith("guardrail_"):
            projection["guardrail_results"].append(
                {"action": action, "outcome": record.get("outcome"), "detail": _redact(record.get("detail", {}))}
            )
        if action in ("proposal_staged", "proposal_schema_invalid"):
            projection["proposal"] = {"action": action, "outcome": record.get("outcome")}
        if action in ("approval_terminal_status", "decision_approved", "decision_rejected", "commit_failed"):
            projection["approval_outcome"] = {"action": action, "outcome": record.get("outcome")}
        projection["events"].append(
            {
                "ts": record.get("ts"),
                "action": action,
                "outcome": record.get("outcome"),
                "actor_type": record.get("actor_type"),
                "actor_id": record.get("actor_id"),
                "model_id": record.get("model_id"),
                "tool_id": record.get("tool_id"),
                "detail": _redact(record.get("detail", {})),
            }
        )
    return projection


def handler(event, _context):
    params = event.get("queryStringParameters") or {}
    interaction_id = params.get("interaction_id") or event.get("interaction_id")
    if not isinstance(interaction_id, str) or not interaction_id:
        return {"statusCode": 400, "body": json.dumps({"error": "interaction_id is required"})}

    result = _ddb.query(
        TableName=AUDIT_TABLE,
        KeyConditionExpression="interaction_id = :i",
        ExpressionAttributeValues={":i": {"S": interaction_id}},
    )
    records = [_decode(item) for item in result.get("Items", [])]
    projection = _project(records)
    return {"statusCode": 200, "body": json.dumps(projection, separators=(",", ":"))}
