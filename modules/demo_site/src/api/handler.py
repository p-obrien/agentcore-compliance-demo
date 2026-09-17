"""Demo web API for the AgentCore compliance training interface.

Three routes back the guided demo:

- ``POST /assess`` relays a signed-in assessor's two Cognito tokens to the
  assessment runtime and returns the buffered draft plus metadata.
- ``GET /audit?interaction_id=`` returns the audit events for one interaction.
- ``GET /audit/recent`` returns recent audit events for the caller's tenant.

The Lambda derives nothing about tenant scope for the assessment call: it
forwards the caller's tokens and the runtime enforces isolation. For the audit
reads it does scope results to the caller's verified tenant, taken from the
access token's ``cognito:groups`` claim (an ``agency-*-assessors`` group), never
from a request field. This keeps one assessor from reading another agency's
audit trail even with a guessed ``interaction_id``.
"""

from __future__ import annotations

import json
import os
import re
import uuid

import assessment_runtime
import audit_read

# Terraform always sets DEMO_ORIGIN to the CloudFront domain. Default to an
# empty origin (which blocks cross-origin) rather than "*" so a missing env var
# fails closed instead of opening the API to every origin.
APPROVAL_ORIGIN = os.environ.get("DEMO_ORIGIN", "")

CORS = {
    "Access-Control-Allow-Origin": APPROVAL_ORIGIN,
    "Access-Control-Allow-Headers": "authorization,content-type,x-id-token",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Content-Type": "application/json",
}

TENANT_GROUPS = {
    "agency-a-assessors": "agency-a",
    "agency-b-assessors": "agency-b",
    "agency-c-assessors": "agency-c",
}


class NotAuthorized(PermissionError):
    """Raised when the token has no subject or no assessor tenant group."""


def _resp(status, body):
    return {"statusCode": status, "headers": CORS, "body": json.dumps(body)}


def _claims(event):
    return (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )


def _header(event, name):
    headers = event.get("headers") or {}
    wanted = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == wanted:
            return value
    return None


def _parse_groups(raw):
    """Normalize a ``cognito:groups`` claim to a list of group names.

    The claim arrives in more than one shape. When the agent decodes a JWT it
    sees a real JSON array. But the API Gateway HTTP API JWT authorizer
    flattens a multi-valued claim to a bracketed, space-separated string such
    as ``[agency-a-assessors agency-a-approvers]`` (unquoted, no commas). The
    earlier ``json.loads`` on a leading ``[`` failed on that form and returned
    an empty list, so every request 403'd with "not an assessor for any
    tenant". Handle all four shapes: Python list, JSON array string, the HTTP
    API bracketed form, and a plain comma/space-delimited string.
    """
    if isinstance(raw, list):
        return [g for g in raw if isinstance(g, str) and g.strip()]
    if not isinstance(raw, str):
        return []
    s = raw.strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [g.strip() for g in parsed if isinstance(g, str) and g.strip()]
        except json.JSONDecodeError:
            pass
        s = s[1:-1] if s.endswith("]") else s[1:]
    return [g for g in re.split(r"[,\s]+", s.strip()) if g]


def _verified_tenant(claims):
    """Resolve the caller's tenant from verified assessor group claims.

    Tenant is read from ``cognito:groups`` because the custom ``tenant_id``
    attribute lives on the ID token, while API Gateway validates the access
    token. An assessor holds exactly one ``agency-*-assessors`` group in this
    demo; the first recognised group wins.
    """
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise NotAuthorized("authenticated token has no subject")
    for group in _parse_groups(claims.get("cognito:groups")):
        tenant = TENANT_GROUPS.get(group)
        if tenant:
            return subject, tenant
    raise NotAuthorized("authenticated user is not an assessor for any tenant")


def _assess(event, claims):
    subject, tenant = _verified_tenant(claims)

    authorization = _header(event, "authorization")
    id_token = _header(event, "x-id-token")
    if not authorization:
        raise NotAuthorized("missing Authorization header")
    if not id_token:
        # The runtime requires the companion ID token; without it the agent
        # denies the session before any content is processed.
        raise ValueError("missing X-Id-Token header")

    body = json.loads(event.get("body") or "{}")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    permit_id = body.get("permit_id")
    if permit_id is not None and not isinstance(permit_id, str):
        raise ValueError("permit_id must be a string")

    # Use a fresh session id per invocation. Reusing the Cognito subject pins
    # every request from a user to one warm AgentCore container, so a new agent
    # image would not take effect until that container idled out. Tenant scope
    # and isolation come from the verified JWT claims, not the session id, so a
    # unique id per call is safe. AgentCore requires at least 33 characters;
    # a uuid4 hex prefixed with the subject satisfies that.
    session_id = f"{subject}-{uuid.uuid4().hex}"

    result = assessment_runtime.invoke_assessment(
        access_authorization=authorization,
        id_token=id_token,
        prompt=prompt,
        permit_id=permit_id or None,
        runtime_session_id=session_id,
    )
    return {
        "verified_tenant": tenant,
        "draft": result.draft,
        "meta": result.meta,
        "agent_error": result.error,
    }


def _audit_interaction(event, claims):
    _subject, tenant = _verified_tenant(claims)
    params = event.get("queryStringParameters") or {}
    interaction_id = params.get("interaction_id")
    if not isinstance(interaction_id, str) or not interaction_id:
        raise ValueError("interaction_id is required")
    events = audit_read.events_for_interaction(interaction_id=interaction_id, tenant_id=tenant)
    return {"tenant_id": tenant, "interaction_id": interaction_id, "events": events}


def _audit_recent(event, claims):
    _subject, tenant = _verified_tenant(claims)
    params = event.get("queryStringParameters") or {}
    limit = params.get("limit")
    events = audit_read.recent_for_tenant(tenant_id=tenant, limit=limit)
    return {"tenant_id": tenant, "events": events}


def handler(event, _context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")
    if method == "OPTIONS":
        return _resp(204, {})
    try:
        claims = _claims(event)
        if method == "POST" and path.endswith("/assess"):
            return _resp(200, _assess(event, claims))
        if method == "GET" and path.endswith("/audit/recent"):
            return _resp(200, _audit_recent(event, claims))
        if method == "GET" and path.endswith("/audit"):
            return _resp(200, _audit_interaction(event, claims))
        return _resp(404, {"error": "not found"})
    except NotAuthorized as exc:
        return _resp(403, {"error": str(exc)})
    except ValueError as exc:
        return _resp(400, {"error": str(exc)})
    except assessment_runtime.RuntimeInvocationError as exc:
        print(f"demo api runtime failure: {exc}")
        return _resp(502, {"error": "assessment runtime call failed"})
    except Exception as exc:  # noqa: BLE001
        print(f"demo api failure: {exc}")
        return _resp(500, {"error": "demo action failed"})
