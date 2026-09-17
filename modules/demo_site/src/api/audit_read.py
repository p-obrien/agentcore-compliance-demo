"""Tenant-scoped, read-only projection of the audit table for the demo UI.

Two queries back the live audit panel: all events for one ``interaction_id``,
and the most recent events for a tenant via the ``by-tenant`` GSI. Both filter
to the caller's verified tenant so a signed-in assessor cannot read another
agency's trail, even by supplying an ``interaction_id`` from a different tenant.
The Lambda role grants only ``dynamodb:Query`` on this table and its index.
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
AUDIT_TABLE = os.environ.get("AUDIT_TABLE", "")
BY_TENANT_INDEX = os.environ.get("AUDIT_BY_TENANT_INDEX", "by-tenant")
RECENT_DEFAULT = 25
RECENT_MAX = 100

_client = None


def _ddb():
    global _client
    if _client is None:
        _client = boto3.client("dynamodb", region_name=REGION)
    return _client


def _item_to_event(item: dict) -> dict[str, Any]:
    detail_raw = item.get("detail", {}).get("S", "")
    try:
        detail = json.loads(detail_raw) if detail_raw else {}
    except json.JSONDecodeError:
        detail = {"raw": detail_raw}
    return {
        "interaction_id": item.get("interaction_id", {}).get("S", ""),
        "ts": item.get("ts", {}).get("N", ""),
        "tenant_id": item.get("tenant_id", {}).get("S", ""),
        "action": item.get("action", {}).get("S", ""),
        "outcome": item.get("outcome", {}).get("S", ""),
        "actor_type": item.get("actor_type", {}).get("S", ""),
        "actor_id": item.get("actor_id", {}).get("S", ""),
        "detail": detail,
    }


def events_for_interaction(*, interaction_id: str, tenant_id: str) -> list[dict[str, Any]]:
    """Return all audit events for one interaction, scoped to ``tenant_id``.

    Any event whose ``tenant_id`` does not match the caller's verified tenant is
    dropped, so a guessed interaction id from another agency yields nothing.
    """
    result = _ddb().query(
        TableName=AUDIT_TABLE,
        KeyConditionExpression="interaction_id = :i",
        ExpressionAttributeValues={":i": {"S": interaction_id}},
        ScanIndexForward=True,
    )
    events = [_item_to_event(item) for item in result.get("Items", [])]
    return [e for e in events if e["tenant_id"] == tenant_id]


def recent_for_tenant(*, tenant_id: str, limit: Any = None) -> list[dict[str, Any]]:
    """Return the most recent audit events for one tenant, newest first."""
    try:
        count = int(limit) if limit is not None else RECENT_DEFAULT
    except (TypeError, ValueError):
        count = RECENT_DEFAULT
    count = max(1, min(count, RECENT_MAX))

    result = _ddb().query(
        TableName=AUDIT_TABLE,
        IndexName=BY_TENANT_INDEX,
        KeyConditionExpression="tenant_id = :t",
        ExpressionAttributeValues={":t": {"S": tenant_id}},
        ScanIndexForward=False,
        Limit=count,
    )
    return [_item_to_event(item) for item in result.get("Items", [])]
