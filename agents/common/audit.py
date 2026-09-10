"""Durable, append-only audit helper and read-only trace projection.

Every action in an interaction appends one immutable Audit_Record keyed by
``interaction_id`` + monotonic ``ts``. Records are only ever written with a
conditional ``PutItem``; this module never emits ``UpdateItem`` or ``DeleteItem``
shapes, so an appended event cannot be mutated.

A trace projection folds all records for one ``Interaction_ID`` into a read-only
view (tenant context, agent, MCP calls, model, guardrail results, proposal, and
approval outcome). The projection strips any task token, bearer token, or secret
so a trace can be shown without leaking a credential.

The record construction and projection logic are pure functions so the property
tests can drive them with an in-memory append-only repository and no live call.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Iterable

import boto3

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

# Actor classes recorded on every event. The tenant assessor agent and the
# retrieval tool are workload actors; the approver is a verified human.
ACTOR_AGENT = "agent"
ACTOR_TOOL = "tool"
ACTOR_APPROVER = "approver"
ACTOR_SYSTEM = "system"

# Detail keys that must never appear in a stored record or a projected trace.
# Matched case-insensitively against a normalized key so ``taskToken`` and
# ``task_token`` are both caught.
_SECRET_KEY_MARKERS = (
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


def _is_secret_key(key: str) -> bool:
    normalized = key.replace("_", "").replace("-", "").lower()
    return any(marker in normalized for marker in _SECRET_KEY_MARKERS)


def redact_secrets(detail: Any) -> Any:
    """Recursively drop task tokens, bearer tokens, and secrets from a detail map.

    Applied both when building a record and when projecting a trace, so a secret
    cannot enter the audit store and cannot leave through a projection even if a
    caller passed one in.
    """
    if isinstance(detail, dict):
        return {
            key: redact_secrets(value)
            for key, value in detail.items()
            if not _is_secret_key(str(key))
        }
    if isinstance(detail, list):
        return [redact_secrets(item) for item in detail]
    return detail


def build_audit_record(
    *,
    interaction_id: str,
    tenant_id: str,
    action: str,
    outcome: str,
    actor_type: str,
    actor_id: str,
    ts_ns: int | None = None,
    model_id: str | None = None,
    tool_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one immutable audit record carrying the required fields.

    Every interaction start/complete, MCP-tool invocation, guardrail input/output,
    staging, decision, and terminal-state event shares this shape and the same
    ``interaction_id``. ``model_id`` and ``tool_id`` are included when the event
    names a model or a tool. Secrets in ``detail`` are dropped before the record
    is returned.
    """
    record: dict[str, Any] = {
        "interaction_id": interaction_id,
        "ts": ts_ns if ts_ns is not None else time.time_ns(),
        "tenant_id": tenant_id,
        "action": action,
        "outcome": outcome,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "detail": redact_secrets(detail or {}),
    }
    if model_id is not None:
        record["model_id"] = model_id
    if tool_id is not None:
        record["tool_id"] = tool_id
    return record


def _to_dynamodb_item(record: dict[str, Any]) -> dict[str, Any]:
    item = {
        "interaction_id": {"S": record["interaction_id"]},
        "ts": {"N": str(record["ts"])},
        "tenant_id": {"S": record["tenant_id"]},
        "action": {"S": record["action"]},
        "outcome": {"S": record["outcome"]},
        "actor_type": {"S": record["actor_type"]},
        "actor_id": {"S": record["actor_id"]},
        "detail": {"S": json.dumps(record["detail"], separators=(",", ":"))[:8000]},
    }
    if "model_id" in record:
        item["model_id"] = {"S": record["model_id"]}
    if "tool_id" in record:
        item["tool_id"] = {"S": record["tool_id"]}
    return item


def append_audit(
    *,
    interaction_id: str,
    tenant_id: str,
    action: str,
    outcome: str,
    actor_type: str = ACTOR_AGENT,
    actor_id: str = "assessment-agent",
    model_id: str | None = None,
    tool_id: str | None = None,
    detail: dict[str, Any] | None = None,
    _client: Any | None = None,
) -> dict[str, Any]:
    """Persist one append-only audit record before an irreversible action.

    The conditional expression rejects a write that would overwrite an existing
    (interaction_id, ts) pair, so records are immutable. Returns the record that
    was written (with secrets already redacted).
    """
    record = build_audit_record(
        interaction_id=interaction_id,
        tenant_id=tenant_id,
        action=action,
        outcome=outcome,
        actor_type=actor_type,
        actor_id=actor_id,
        model_id=model_id,
        tool_id=tool_id,
        detail=detail,
    )
    client = _client or _ddb()
    client.put_item(
        TableName=_audit_table(),
        Item=_to_dynamodb_item(record),
        ConditionExpression="attribute_not_exists(interaction_id) AND attribute_not_exists(ts)",
    )
    return record


def project_trace(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Fold audit records for one Interaction_ID into a read-only trace view.

    Accepts already-decoded records (as returned by ``build_audit_record`` or a
    query deserializer). Records are ordered by timestamp. The projection carries
    the tenant, ordered events, and named slots for the tenant context, model,
    MCP calls, guardrail results, proposal, and approval outcome. Secrets are
    stripped defensively even though ``build_audit_record`` already dropped them.
    """
    ordered = sorted(records, key=lambda r: r.get("ts", 0))
    projection: dict[str, Any] = {
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
        projection["interaction_id"] = projection["interaction_id"] or record.get(
            "interaction_id"
        )
        projection["tenant_id"] = projection["tenant_id"] or record.get("tenant_id")

        model_id = record.get("model_id")
        if model_id and model_id not in projection["model_ids"]:
            projection["model_ids"].append(model_id)
        if record.get("actor_type") == ACTOR_TOOL or record.get("tool_id"):
            projection["mcp_calls"].append(
                {"action": action, "tool_id": record.get("tool_id"), "outcome": record.get("outcome")}
            )
        if action.startswith("guardrail_"):
            projection["guardrail_results"].append(
                {"action": action, "outcome": record.get("outcome"), "detail": redact_secrets(record.get("detail", {}))}
            )
        if action in ("proposal_staged", "proposal_schema_invalid"):
            projection["proposal"] = {"action": action, "outcome": record.get("outcome")}
        if action in ("decision_approved", "decision_rejected", "commit_failed"):
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
                "detail": redact_secrets(record.get("detail", {})),
            }
        )
    return projection


def _audit_table() -> str:
    return os.environ["AUDIT_TABLE"]


def _ddb():
    return boto3.client("dynamodb", region_name=REGION)
