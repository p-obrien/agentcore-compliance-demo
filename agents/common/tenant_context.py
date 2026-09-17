"""Verified Cognito identity and signed retrieval session capabilities.

Tenant and ACL scope are derived only from Cognito tokens that AgentCore Runtime
accepted at ingress. Request JSON, prompt text, and model output never decide a
tenant. The runtime creates a five-minute HMAC capability for the Gateway tool;
the retrieval Lambda verifies the same capability before selecting an Elastic
DLS credential.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import boto3
import jwt

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
COGNITO_ISSUER = os.environ["COGNITO_ISSUER"]
COGNITO_AGENT_CLIENT_ID = os.environ["COGNITO_AGENT_CLIENT_ID"]
SESSION_CONTEXT_SECRET_ARN = os.environ["SESSION_CONTEXT_SECRET_ARN"]
SESSION_TTL_SECONDS = 300

TENANT_GROUPS = {
    "agency-a": "agency-a-assessors",
    "agency-b": "agency-b-assessors",
    "agency-c": "agency-c-assessors",
}

# Assurance tier per tenant. Agencies A and B share one managed domain; Agency C
# uses a dedicated managed domain guarded by a resource-based access policy.
TENANT_TIERS = {
    "agency-a": "shared",
    "agency-b": "shared",
    "agency-c": "dedicated",
}

_secrets = boto3.client("secretsmanager", region_name=REGION)
_jwks = jwt.PyJWKClient(f"{COGNITO_ISSUER}/.well-known/jwks.json")
_session_key: bytes | None = None


class NoTenantContextError(ValueError):
    """Raised when an inbound request lacks a valid, tenant-bound identity.

    The session is denied before any prompt or MCP input is processed. The
    stable machine ``reason`` is ``identity_denied`` so callers and audit
    records can classify the denial without parsing the human message. No raw
    JWT or token text is ever placed in the message or reason.
    """

    reason: str = "identity_denied"


@dataclass(frozen=True)
class VerifiedIdentity:
    """Identity claims that passed Cognito signature and claim validation.

    Retained as the return shape of :func:`resolve_tenant` for backward
    compatibility with ``intake/agent.py`` and ``assessment/agent.py``, which
    read ``.subject``, ``.tenant_id``, ``.allowed_groups``, and ``.access_token``.
    """

    subject: str
    tenant_id: str
    allowed_groups: tuple[str, ...]
    access_token: str


@dataclass(frozen=True)
class Tenant_Context:
    """The one tenant context derived solely from verified identity claims.

    This is the ``Identity and tenant context`` data model from the design: it
    is built before ``prompt``, ``context``, or MCP inputs are read, and it
    never draws any field from request body, prompt text, or model output.
    """

    interaction_id: str
    tenant_id: str
    tier: str
    subject: str
    document_acl_groups: frozenset[str]
    agentcore_tool_scope: frozenset[str]
    issued_at: int
    expires_at: int


def _header(request: dict[str, Any], name: str) -> str | None:
    headers = request.get("headers") or {}
    if not isinstance(headers, dict):
        return None
    wanted = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == wanted and isinstance(value, str):
            return value
    return None


def _bearer(value: str | None) -> str:
    if not isinstance(value, str) or not value.lower().startswith("bearer "):
        raise NoTenantContextError("missing bearer access token")
    token = value[7:].strip()
    if not token:
        raise NoTenantContextError("missing bearer access token")
    return token


def _decode(token: str, *, audience: str | None) -> dict[str, Any]:
    try:
        signing_key = _jwks.get_signing_key_from_jwt(token).key
        options = {"require": ["exp", "iat", "sub", "iss"]}
        return jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=COGNITO_ISSUER,
            audience=audience,
            options={**options, "verify_aud": audience is not None},
        )
    except jwt.PyJWTError as exc:
        raise NoTenantContextError("invalid Cognito token") from exc


def _groups(claims: dict[str, Any]) -> set[str]:
    groups = claims.get("cognito:groups", [])
    if isinstance(groups, str):
        s = groups.strip()
        parsed: list[str] = []
        if s.startswith("["):
            try:
                loaded = json.loads(s)
                if isinstance(loaded, list):
                    parsed = [g for g in loaded if isinstance(g, str)]
                    s = ""
            except json.JSONDecodeError:
                # HTTP API flattens multi-valued claims to '[a b c]'
                # (unquoted, space-separated); strip brackets then split.
                s = s[1:-1] if s.endswith("]") else s[1:]
        if s:
            parsed = re.split(r"[,\s]+", s.strip())
        groups = parsed
    if not isinstance(groups, list):
        return set()
    return {group.strip() for group in groups if isinstance(group, str) and group.strip()}


def resolve_tenant(request: dict[str, Any]) -> VerifiedIdentity:
    """Verify companion Cognito tokens and derive the only permitted tenant.

    `X-Id-Token` is explicitly forwarded by AgentCore Runtime. AgentCore
    consumes the inbound `Authorization` header for its own JWT authorizer and
    does not forward it to the agent, so the caller also sends the access token
    as `X-Access-Token`. Read that first and fall back to the `Authorization`
    bearer for callers on a path where it is forwarded. Both tokens must belong
    to the same Cognito subject and the configured agent client.
    """
    id_token = _header(request, "X-Id-Token")
    access_token = _header(request, "X-Access-Token") or _bearer(_header(request, "Authorization"))
    if not id_token:
        raise NoTenantContextError("missing Cognito ID token")

    id_claims = _decode(id_token, audience=COGNITO_AGENT_CLIENT_ID)
    access_claims = _decode(access_token, audience=None)
    if id_claims.get("token_use") != "id":
        raise NoTenantContextError("expected Cognito ID token")
    if access_claims.get("token_use") != "access":
        raise NoTenantContextError("expected Cognito access token")
    if access_claims.get("client_id") != COGNITO_AGENT_CLIENT_ID:
        raise NoTenantContextError("access token is for a different client")
    if id_claims.get("sub") != access_claims.get("sub"):
        raise NoTenantContextError("Cognito token subjects do not match")

    tenant_id = id_claims.get("custom:tenant_id")
    expected_group = TENANT_GROUPS.get(tenant_id)
    groups = _groups(id_claims)
    # Deny before content processing when the tenant is outside the allowlist
    # (agency-a/agency-b/agency-c) or the required assessor scope is absent.
    if not isinstance(tenant_id, str) or not expected_group or expected_group not in groups:
        raise NoTenantContextError("identity has no authorised tenant assessor group")

    return VerifiedIdentity(
        subject=id_claims["sub"],
        tenant_id=tenant_id,
        allowed_groups=(expected_group,),
        access_token=access_token,
    )


def tenant_context_for(identity: VerifiedIdentity, interaction_id: str) -> Tenant_Context:
    """Derive the design ``Tenant_Context`` from an already-verified identity.

    Every field comes from verified claims. The tier is looked up from the
    authenticated tenant; the ACL groups and AgentCore tool scope are the
    tenant assessor groups that passed verification. No value is taken from any
    caller-supplied field.
    """
    tier = TENANT_TIERS.get(identity.tenant_id)
    if tier is None:
        raise NoTenantContextError("identity has no authorised tenant assessor group")
    now = int(time.time())
    groups = frozenset(identity.allowed_groups)
    return Tenant_Context(
        interaction_id=interaction_id,
        tenant_id=identity.tenant_id,
        tier=tier,
        subject=identity.subject,
        document_acl_groups=groups,
        agentcore_tool_scope=groups,
        issued_at=now,
        expires_at=now + SESSION_TTL_SECONDS,
    )


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _load_session_key() -> bytes:
    global _session_key
    if _session_key is None:
        secret = _secrets.get_secret_value(SecretId=SESSION_CONTEXT_SECRET_ARN)["SecretString"]
        _session_key = json.loads(secret)["key"].encode("utf-8")
    return _session_key


def _mint_capability(context: Tenant_Context) -> str:
    """Sign an opaque HMAC-SHA256 capability from a verified ``Tenant_Context``.

    Every field is copied from the already-verified context, so no prompt text,
    MCP argument, or model output can supply or alter a capability field. The
    expiry is clamped to at most ``iat + SESSION_TTL_SECONDS`` regardless of the
    context's own ``expires_at``, enforcing the five-minute maximum defensively.
    """
    iat = context.issued_at
    exp = min(context.expires_at, iat + SESSION_TTL_SECONDS)
    payload = {
        "subject": context.subject,
        "tenant_id": context.tenant_id,
        "allowed_groups": sorted(context.document_acl_groups),
        "agentcore_tool_scope": sorted(context.agentcore_tool_scope),
        "interaction_id": context.interaction_id,
        "iat": iat,
        "exp": exp,
    }
    encoded = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(_load_session_key(), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64url(signature)}"


def session_context_for_tool(identity: VerifiedIdentity, interaction_id: str) -> str:
    """Mint a five-minute, signed capability consumed only by retrieval.

    The capability is derived strictly from the verified ``Tenant_Context`` built
    by :func:`tenant_context_for`. Callers pass only the verified identity and the
    runtime-generated interaction id; no request body, prompt, or MCP argument can
    reach the minted payload.
    """
    context = tenant_context_for(identity, interaction_id)
    return _mint_capability(context)
