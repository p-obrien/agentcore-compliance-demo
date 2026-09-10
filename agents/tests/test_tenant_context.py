"""Property and unit tests for trusted tenant-context resolution.

The module under test is ``agents/common/tenant_context.py``. It reads Cognito
configuration and builds a ``PyJWKClient`` plus a Secrets Manager client at
import time, so this module sets a synthetic environment and stubs both
boundaries before importing it. Tokens are signed with the deterministic RSA
keys from the shared ``jwt_signing_keys`` fixture, and the module's JWKS lookup
is patched to return the matching public key. No network call or live AWS call
is made.

Covers task 2 sub-tasks:
  - 2.3 Property 1: Trusted tenant-context noninterference
  - 2.4 Property 2: Invalid identity fails before content processing
  - 2.5 Unit tests: group parsing and claim edge cases
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any

import jwt
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_TENANT_CONTEXT_PATH = (
    Path(__file__).resolve().parents[1] / "common" / "tenant_context.py"
)

_ISSUER = "https://cognito-idp.ap-southeast-2.amazonaws.com/ap-southeast-2_TEST"
_AGENT_CLIENT_ID = "agent-client-id-123"
_SESSION_SECRET_ARN = "arn:aws:secretsmanager:ap-southeast-2:111111111111:secret:sess"
_SESSION_KEY = "unit-test-session-signing-key"

_TENANTS = ("agency-a", "agency-b", "agency-c")


class _FakeSecrets:
    def get_secret_value(self, SecretId: str) -> dict[str, str]:  # noqa: N803
        import json

        return {"SecretString": json.dumps({"key": _SESSION_KEY})}


class _StubJWKClient:
    """Stands in for ``jwt.PyJWKClient``; resolves signing keys from a registry.

    The registry is populated per test from the generated RSA fixture keys, so
    ``get_signing_key_from_jwt`` never reaches a JWKS endpoint.
    """

    registry: dict[str, Any] = {}

    def __init__(self, _url: str) -> None:
        pass

    def get_signing_key_from_jwt(self, token: str):
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        key = self.registry.get(kid)
        if key is None:
            raise jwt.PyJWTError(f"no signing key for kid {kid!r}")
        return type("SigningKey", (), {"key": key})()


@pytest.fixture(scope="module")
def tc_module():
    """Import tenant_context once with a synthetic env and stubbed boundaries."""
    import os

    import boto3

    saved_env = dict(os.environ)
    saved_client = boto3.client
    saved_jwk = jwt.PyJWKClient
    os.environ.update(
        {
            "COGNITO_ISSUER": _ISSUER,
            "COGNITO_AGENT_CLIENT_ID": _AGENT_CLIENT_ID,
            "SESSION_CONTEXT_SECRET_ARN": _SESSION_SECRET_ARN,
            "AWS_REGION": "ap-southeast-2",
        }
    )
    boto3.client = lambda service, **_k: _FakeSecrets()  # type: ignore[assignment]
    jwt.PyJWKClient = _StubJWKClient  # type: ignore[assignment]
    try:
        spec = importlib.util.spec_from_file_location(
            "tenant_context_under_test", _TENANT_CONTEXT_PATH
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["tenant_context_under_test"] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        # Ensure the module's JWKS handle is our stub instance.
        module._jwks = _StubJWKClient(f"{_ISSUER}/.well-known/jwks.json")
        yield module
    finally:
        boto3.client = saved_client  # type: ignore[assignment]
        jwt.PyJWKClient = saved_jwk  # type: ignore[assignment]
        os.environ.clear()
        os.environ.update(saved_env)
        sys.modules.pop("tenant_context_under_test", None)


@pytest.fixture
def register_key(jwt_signing_keys):
    """Register the first fixture key in the stub JWKS registry for signing.

    Returns a ``(kid, private_pem)`` pair the test uses to sign tokens; the
    matching public key is what the stub returns to the verifier.
    """
    from cryptography.hazmat.primitives import serialization

    key = jwt_signing_keys[0]
    public_key = serialization.load_pem_public_key(key.public_pem)
    _StubJWKClient.registry = {key.kid: public_key}
    return key.kid, key.private_pem


def _sign(private_pem: bytes, kid: str, claims: dict[str, Any]) -> str:
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": kid})


def _id_claims(tenant_id: str, subject: str, groups: list[str]) -> dict[str, Any]:
    now = int(time.time())
    return {
        "iss": _ISSUER,
        "aud": _AGENT_CLIENT_ID,
        "sub": subject,
        "token_use": "id",
        "custom:tenant_id": tenant_id,
        "cognito:groups": groups,
        "iat": now,
        "exp": now + 300,
    }


def _access_claims(subject: str) -> dict[str, Any]:
    now = int(time.time())
    return {
        "iss": _ISSUER,
        "sub": subject,
        "token_use": "access",
        "client_id": _AGENT_CLIENT_ID,
        "iat": now,
        "exp": now + 300,
    }


def _request(id_token: str, access_token: str) -> dict[str, Any]:
    return {
        "headers": {
            "X-Id-Token": id_token,
            "Authorization": f"Bearer {access_token}",
        }
    }


# ---------------------------------------------------------------------------
# 2.3 Property 1: Trusted tenant-context noninterference
# ---------------------------------------------------------------------------


@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    tenant_id=st.sampled_from(_TENANTS),
    subject=st.text(min_size=1, max_size=16).filter(lambda s: s.strip()),
    # Arbitrary caller-supplied tenant/ACL values that must not influence output.
    spoof_tenant=st.sampled_from(["agency-b", "agency-c", "attacker", ""]),
    spoof_groups=st.lists(st.text(max_size=12), max_size=4),
    prompt=st.text(max_size=40),
)
def test_property_1_tenant_context_noninterference(
    tc_module, register_key, tenant_id, subject, spoof_tenant, spoof_groups, prompt
):
    # Feature: agentcore-demo-lab, Property 1: For any valid authenticated claim
    # set for one known tenant and any prompt, MCP arguments, request metadata,
    # or model-produced tenant/ACL values, tenant-context resolution shall
    # produce exactly one context whose tenant, subject, ACL groups, and tool
    # scope equal the verified claims and are unaffected by caller-supplied
    # values.
    kid, private_pem = register_key
    expected_group = tc_module.TENANT_GROUPS[tenant_id]
    id_token = _sign(private_pem, kid, _id_claims(tenant_id, subject, [expected_group]))
    access_token = _sign(private_pem, kid, _access_claims(subject))

    request = _request(id_token, access_token)
    # Inject caller-controllable spoof values into the request body/prompt; none
    # of these are inputs the resolver reads.
    request["prompt"] = prompt
    request["tenant_id"] = spoof_tenant
    request["allowed_groups"] = spoof_groups
    request["arguments"] = {"tenant_id": spoof_tenant, "acl": spoof_groups}

    identity = tc_module.resolve_tenant(request)
    context = tc_module.tenant_context_for(identity, interaction_id="int-noninterference")

    assert context.tenant_id == tenant_id
    assert context.subject == subject
    assert context.document_acl_groups == frozenset({expected_group})
    assert context.agentcore_tool_scope == frozenset({expected_group})
    assert context.tier == tc_module.TENANT_TIERS[tenant_id]
    # The verified access token flows through unchanged for downstream reuse.
    assert identity.tenant_id == tenant_id
    assert identity.allowed_groups == (expected_group,)


# ---------------------------------------------------------------------------
# 2.4 Property 2: Invalid identity fails before content processing
# ---------------------------------------------------------------------------


@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    tenant_id=st.sampled_from(["agency-a", "agency-b", "agency-c", "agency-x", "unknown", ""]),
    groups=st.lists(
        st.sampled_from(
            [
                "agency-a-assessors",
                "agency-b-assessors",
                "agency-c-assessors",
                "some-other-group",
                "demo-approvers",
            ]
        ),
        max_size=3,
    ),
    subject=st.text(min_size=1, max_size=12).filter(lambda s: s.strip()),
)
def test_property_2_invalid_identity_denied_early(
    tc_module, register_key, tenant_id, groups, subject
):
    # Feature: agentcore-demo-lab, Property 2: For any claim set whose tenant is
    # not in the allowlist or whose groups omit the tenant's required assessor
    # scope, the identity resolver shall deny the session before invoking prompt
    # parsing, model invocation, or MCP-tool construction.
    kid, private_pem = register_key
    expected_group = tc_module.TENANT_GROUPS.get(tenant_id)
    is_valid = expected_group is not None and expected_group in groups

    id_token = _sign(private_pem, kid, _id_claims(tenant_id, subject, groups))
    access_token = _sign(private_pem, kid, _access_claims(subject))
    request = _request(id_token, access_token)

    if is_valid:
        # Guard: a coincidentally valid combination must resolve, not deny.
        identity = tc_module.resolve_tenant(request)
        assert identity.tenant_id == tenant_id
    else:
        with pytest.raises(tc_module.NoTenantContextError) as exc_info:
            tc_module.resolve_tenant(request)
        # Denial carries the stable machine reason and never a raw token.
        assert exc_info.value.reason == "identity_denied"
        assert access_token not in str(exc_info.value)
        assert id_token not in str(exc_info.value)


# ---------------------------------------------------------------------------
# 2.5 Unit tests: group parsing and claim edge cases
# ---------------------------------------------------------------------------


def test_groups_parsed_from_json_string(tc_module, register_key):
    """A JSON-encoded cognito:groups string is parsed, not treated as opaque."""
    import json

    kid, private_pem = register_key
    claims = _id_claims("agency-a", "user-1", [])
    claims["cognito:groups"] = json.dumps(["agency-a-assessors"])
    id_token = _sign(private_pem, kid, claims)
    access_token = _sign(private_pem, kid, _access_claims("user-1"))

    identity = tc_module.resolve_tenant(_request(id_token, access_token))
    assert identity.allowed_groups == ("agency-a-assessors",)


def test_subject_mismatch_denied(tc_module, register_key):
    """ID and access tokens for different subjects are rejected."""
    kid, private_pem = register_key
    id_token = _sign(
        private_pem, kid, _id_claims("agency-a", "user-1", ["agency-a-assessors"])
    )
    access_token = _sign(private_pem, kid, _access_claims("user-2"))
    with pytest.raises(tc_module.NoTenantContextError):
        tc_module.resolve_tenant(_request(id_token, access_token))


def test_wrong_client_id_denied(tc_module, register_key):
    """An access token minted for a different app client is rejected."""
    kid, private_pem = register_key
    id_token = _sign(
        private_pem, kid, _id_claims("agency-a", "user-1", ["agency-a-assessors"])
    )
    access = _access_claims("user-1")
    access["client_id"] = "some-other-client"
    access_token = _sign(private_pem, kid, access)
    with pytest.raises(tc_module.NoTenantContextError):
        tc_module.resolve_tenant(_request(id_token, access_token))


def test_expired_token_denied(tc_module, register_key):
    """An expired ID token is rejected by signature/claim validation."""
    kid, private_pem = register_key
    claims = _id_claims("agency-a", "user-1", ["agency-a-assessors"])
    claims["iat"] = int(time.time()) - 3600
    claims["exp"] = int(time.time()) - 60
    id_token = _sign(private_pem, kid, claims)
    access_token = _sign(private_pem, kid, _access_claims("user-1"))
    with pytest.raises(tc_module.NoTenantContextError):
        tc_module.resolve_tenant(_request(id_token, access_token))


def test_missing_id_token_denied(tc_module, register_key):
    """The companion X-Id-Token header is required."""
    kid, private_pem = register_key
    access_token = _sign(private_pem, kid, _access_claims("user-1"))
    request = {"headers": {"Authorization": f"Bearer {access_token}"}}
    with pytest.raises(tc_module.NoTenantContextError):
        tc_module.resolve_tenant(request)


def test_capability_expiry_clamped_to_five_minutes(tc_module, register_key):
    """A minted capability never carries more than a five-minute lifetime."""
    kid, private_pem = register_key
    id_token = _sign(
        private_pem, kid, _id_claims("agency-a", "user-1", ["agency-a-assessors"])
    )
    access_token = _sign(private_pem, kid, _access_claims("user-1"))
    identity = tc_module.resolve_tenant(_request(id_token, access_token))
    token = tc_module.session_context_for_tool(identity, interaction_id="int-clamp")

    import base64
    import json

    encoded = token.split(".", 1)[0]
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    assert 0 < payload["exp"] - payload["iat"] <= tc_module.SESSION_TTL_SECONDS
    assert payload["tenant_id"] == "agency-a"
    assert payload["allowed_groups"] == ["agency-a-assessors"]
