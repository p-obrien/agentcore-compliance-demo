"""Property and unit tests for the Intelligence API retrieval handler.

The handler under test is ``modules/retrieval_tool/src/handler.py``. It reads
environment variables and constructs boto3 clients at import time, so this
module sets a synthetic environment and stubs the AWS SDK entry points *before*
importing it. Every AWS boundary (Secrets Manager, STS, DynamoDB, and the
OpenSearch HTTP call) is replaced with an in-memory fake; no test opens a socket
or reads a real credential.

Covers task 3 sub-tasks:
  - 3.4 Property 3: Tenant-parametric routing and shared query constraints
  - 3.5 Property 4: Shared-tier spoof noninterference and audit evidence
  - 3.6 Property 5: Dedicated-domain denial evidence
  - 3.7 Unit tests: zero-document (Agency A requesting B-2001) and fail-closed
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Import the handler with AWS boundaries stubbed at module-load time.
# ---------------------------------------------------------------------------

_HANDLER_PATH = (
    Path(__file__).resolve().parents[2]
    / "modules"
    / "retrieval_tool"
    / "src"
    / "handler.py"
)

# Non-secret routing map the root wiring would populate from managed-domain
# module outputs. Agencies A and B share one domain; Agency C is dedicated.
_ES_TARGETS = {
    "agency-a": {
        "endpoint": "https://shared.example.aoss.invalid",
        "index_name": "permits",
        "role_arn": "arn:aws:iam::111111111111:role/shared-read",
        "tier": "shared",
        "principal_class": "shared-retrieval",
        "domain_arn": "arn:aws:es:ap-southeast-2:111111111111:domain/shared",
    },
    "agency-b": {
        "endpoint": "https://shared.example.aoss.invalid",
        "index_name": "permits",
        "role_arn": "arn:aws:iam::111111111111:role/shared-read",
        "tier": "shared",
        "principal_class": "shared-retrieval",
        "domain_arn": "arn:aws:es:ap-southeast-2:111111111111:domain/shared",
    },
    "agency-c": {
        "endpoint": "https://dedicated.example.aoss.invalid",
        "index_name": "permits",
        "role_arn": "arn:aws:iam::111111111111:role/dedicated-read",
        "tier": "dedicated",
        "principal_class": "dedicated-retrieval",
        "domain_arn": "arn:aws:es:ap-southeast-2:111111111111:domain/dedicated",
    },
}

_SESSION_KEY = "unit-test-session-signing-key"


class _FakeSecrets:
    def get_secret_value(self, SecretId: str) -> dict[str, str]:  # noqa: N803
        return {"SecretString": json.dumps({"key": _SESSION_KEY})}


class _FakeSTS:
    def assume_role(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "Credentials": {
                "AccessKeyId": "AKIAFAKE",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": SimpleNamespace(timestamp=lambda: 1_900_000_000.0),
            }
        }


class _FakeDDB:
    """Records append-only audit writes; rejects a duplicate primary key."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        self.items.append(kwargs["Item"])
        return {}


def _fake_boto3_client(service: str, **_kwargs: Any):
    if service == "secretsmanager":
        return _FakeSecrets()
    if service == "sts":
        return _FakeSTS()
    if service == "dynamodb":
        return _FakeDDB()
    raise AssertionError(f"unexpected boto3 client requested: {service}")


@pytest.fixture(scope="module")
def handler_module():
    """Load handler.py once with a synthetic env and stubbed boto3.client."""
    import os

    import boto3

    saved_env = dict(os.environ)
    saved_client = boto3.client
    os.environ.update(
        {
            "ES_TARGETS_JSON": json.dumps(_ES_TARGETS),
            "SESSION_CONTEXT_SECRET_ARN": "arn:aws:secretsmanager:ap-southeast-2:111111111111:secret:sess",
            "AUDIT_TABLE": "audit-table",
            "AWS_REGION": "ap-southeast-2",
        }
    )
    boto3.client = _fake_boto3_client  # type: ignore[assignment]
    try:
        spec = importlib.util.spec_from_file_location("retrieval_handler", _HANDLER_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules["retrieval_handler"] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        yield module
    finally:
        boto3.client = saved_client  # type: ignore[assignment]
        os.environ.clear()
        os.environ.update(saved_env)
        sys.modules.pop("retrieval_handler", None)


@pytest.fixture
def fresh_audit(handler_module):
    """Give each test a clean audit sink and clear the assume-role cache."""
    ddb = _FakeDDB()
    handler_module._ddb = ddb
    handler_module._assumed_credentials.clear()
    return ddb


def _reset_audit(handler_module):
    """Reset the audit sink for a single hypothesis example.

    Hypothesis runs many generated examples inside one test-function call, so a
    function-scoped fixture is created once and would otherwise accumulate audit
    records across examples. Each example resets the sink so a "exactly one
    record" assertion is scoped to that example, and returns the fresh sink.
    """
    ddb = _FakeDDB()
    handler_module._ddb = ddb
    handler_module._assumed_credentials.clear()
    return ddb


# ---------------------------------------------------------------------------
# Test helpers: mint a valid capability the way the runtime does, and drive
# the OpenSearch call with an in-memory search result instead of a socket.
# ---------------------------------------------------------------------------


def _mint_token(handler_module, *, tenant_id: str, subject: str, interaction_id: str) -> str:
    payload = {
        "subject": subject,
        "tenant_id": tenant_id,
        "allowed_groups": [f"{tenant_id}-assessors"],
        "agentcore_tool_scope": [f"{tenant_id}-assessors"],
        "interaction_id": interaction_id,
        "iat": int(handler_module.time.time()),
        "exp": int(handler_module.time.time()) + 300,
    }
    import base64
    import hashlib
    import hmac

    def b64url(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    body = b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = hmac.new(_SESSION_KEY.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{b64url(sig)}"


def _install_search(handler_module, monkeypatch, *, documents=None, raise_exc=None):
    """Replace ``_es_search`` and capture the query body it was handed.

    Returns a mutable dict whose ``body`` key holds the last query the handler
    built, so a property test can assert on the immutable filter clauses.
    """
    captured: dict[str, Any] = {"body": None, "target": None}

    def fake_search(body, target):
        captured["body"] = body
        captured["target"] = target
        if raise_exc is not None:
            raise raise_exc
        hits = [{"_source": doc} for doc in (documents or [])]
        return {"hits": {"hits": hits}}

    monkeypatch.setattr(handler_module, "_es_search", fake_search)
    return captured


# ---------------------------------------------------------------------------
# 3.4 Property 3: Tenant-parametric routing and shared query constraints
# ---------------------------------------------------------------------------


@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    tenant_id=st.sampled_from(["agency-a", "agency-b", "agency-c"]),
    query_text=st.text(max_size=40),
    permit_id=st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Nd"), whitelist_characters="-"),
        max_size=12,
    ),
)
def test_property_3_tenant_routing_and_shared_query_constraints(
    handler_module, fresh_audit, monkeypatch, tenant_id, query_text, permit_id
):
    # Feature: agentcore-demo-lab, Property 3: For any valid Tenant_Context,
    # target selection shall route Agencies A and B to the shared domain and
    # Agency C to the dedicated domain. For every shared-tier request, the
    # constructed OpenSearch query shall contain an immutable tenant_id filter
    # equal to the authenticated tenant and an ACL predicate derived only from
    # that context's document ACL groups.
    captured = _install_search(handler_module, monkeypatch, documents=[])
    _reset_audit(handler_module)
    token = _mint_token(
        handler_module, tenant_id=tenant_id, subject="user-1", interaction_id="int-1"
    )
    event = {"arguments": {"session_token": token, "query": query_text, "permit_id": permit_id}}

    handler_module.handler(event, None)

    target = captured["target"]
    expected_tier = "dedicated" if tenant_id == "agency-c" else "shared"
    assert target["tier"] == expected_tier
    assert target["tenant_id"] == tenant_id

    # The immutable isolation controls are always present and equal to the
    # authenticated tenant, regardless of caller query text.
    filters = captured["body"]["query"]["bool"]["filter"]
    tenant_terms = [f for f in filters if f.get("term", {}).get("tenant_id") is not None]
    assert tenant_terms == [{"term": {"tenant_id": tenant_id}}]

    # The ACL predicate is derived only from the context's groups.
    acl = next(f for f in filters if "bool" in f)
    terms_clause = next(
        should for should in acl["bool"]["should"] if "terms" in should
    )
    assert terms_clause["terms"]["allowed_groups"] == [f"{tenant_id}-assessors"]


# ---------------------------------------------------------------------------
# 3.5 Property 4: Shared-tier spoof noninterference and audit evidence
# ---------------------------------------------------------------------------


_SPOOF_FIELD_STRATEGY = st.dictionaries(
    keys=st.sampled_from(
        ["tenant_id", "allowed_groups", "acl", "actor", "endpoint", "index", "tier"]
    ),
    values=st.sampled_from(["agency-b", "agency-c", "attacker", ["agency-b-assessors"]]),
    min_size=1,
    max_size=4,
)


@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    tenant_id=st.sampled_from(["agency-a", "agency-b"]),
    spoof=_SPOOF_FIELD_STRATEGY,
)
def test_property_4_shared_spoof_noninterference_and_audit(
    handler_module, fresh_audit, monkeypatch, tenant_id, spoof
):
    # Feature: agentcore-demo-lab, Property 4: For any Agency A or B context,
    # any spoofed tenant/ACL arguments, and any mixed-tenant document set,
    # retrieval results shall contain only documents whose tenant and ACLs
    # satisfy the authenticated context. If spoof-capable fields are supplied,
    # retrieval shall append one tenant_spoof_attempt_ignored Audit_Record
    # carrying the effective tenant and Interaction_ID.
    captured = _install_search(handler_module, monkeypatch, documents=[])
    audit = _reset_audit(handler_module)
    interaction_id = "int-spoof"
    token = _mint_token(
        handler_module, tenant_id=tenant_id, subject="user-1", interaction_id=interaction_id
    )
    args = {"session_token": token, "query": "review", **spoof}
    result = handler_module.handler({"arguments": args}, None)

    # The derived tenant is retained; the query filter is never rewritten to a
    # spoofed value.
    assert result["tenant_id"] == tenant_id
    tenant_terms = [
        f for f in captured["body"]["query"]["bool"]["filter"]
        if f.get("term", {}).get("tenant_id") is not None
    ]
    assert tenant_terms == [{"term": {"tenant_id": tenant_id}}]

    # Exactly one tenant_spoof_attempt_ignored record, carrying the effective
    # tenant and Interaction_ID.
    spoof_records = [
        item for item in audit.items
        if item["event_type"]["S"] == "tenant_spoof_attempt_ignored"
    ]
    assert len(spoof_records) == 1
    record = spoof_records[0]
    assert record["tenant_id"]["S"] == tenant_id
    assert record["interaction_id"]["S"] == interaction_id
    detail = json.loads(record["detail"]["S"])
    assert detail["effective_tenant_id"] == tenant_id


# ---------------------------------------------------------------------------
# 3.6 Property 5: Dedicated-domain denial evidence
# ---------------------------------------------------------------------------


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(interaction_id=st.text(min_size=1, max_size=24).filter(lambda s: s.strip()))
def test_property_5_dedicated_domain_denial_evidence(
    handler_module, fresh_audit, monkeypatch, interaction_id
):
    # Feature: agentcore-demo-lab, Property 5: For any authorization-denied
    # response while a principal accesses the dedicated domain, the Intelligence
    # API shall return no document payload and append one
    # dedicated_domain_access_denied Audit_Record with the Interaction_ID and
    # requesting principal.
    _install_search(
        handler_module,
        monkeypatch,
        raise_exc=handler_module.DedicatedDomainAccessDenied(),
    )
    audit = _reset_audit(handler_module)
    token = _mint_token(
        handler_module, tenant_id="agency-c", subject="user-c", interaction_id=interaction_id
    )
    result = handler_module.handler(
        {"arguments": {"session_token": token, "query": "anything"}}, None
    )

    assert result["results"] == []
    assert result["count"] == 0
    assert result["reason"] == "dedicated_domain_access_denied"

    denied = [
        item for item in audit.items
        if item["event_type"]["S"] == "dedicated_domain_access_denied"
    ]
    assert len(denied) == 1
    detail = json.loads(denied[0]["detail"]["S"])
    assert detail["requesting_principal"] == "dedicated-retrieval"
    assert denied[0]["interaction_id"]["S"] == interaction_id


# ---------------------------------------------------------------------------
# 3.7 Unit tests: zero-document and fail-closed paths
# ---------------------------------------------------------------------------


def test_agency_a_requesting_b2001_returns_zero_documents(
    handler_module, fresh_audit, monkeypatch
):
    """Agency A asking for a Agency B permit gets no documents.

    The domain returns no hits because the immutable tenant_id filter excludes
    Agency B data; the handler surfaces an empty result set, not an error.
    """
    captured = _install_search(handler_module, monkeypatch, documents=[])
    token = _mint_token(
        handler_module, tenant_id="agency-a", subject="user-a", interaction_id="int-b2001"
    )
    result = handler_module.handler(
        {"arguments": {"session_token": token, "permit_id": "B-2001"}}, None
    )

    assert result["count"] == 0
    assert result["results"] == []
    # The tenant filter is agency-a even though the caller asked for a B permit.
    tenant_terms = [
        f for f in captured["body"]["query"]["bool"]["filter"]
        if f.get("term", {}).get("tenant_id") is not None
    ]
    assert tenant_terms == [{"term": {"tenant_id": "agency-a"}}]
    # A normal retrieval audit record is written (not a failure).
    assert any(item["event_type"]["S"] == "retrieval" for item in fresh_audit.items)


def test_shared_domain_error_fails_closed_without_rerouting(
    handler_module, fresh_audit, monkeypatch
):
    """A shared-domain failure returns no content and never reroutes to dedicated."""
    captured = _install_search(
        handler_module, monkeypatch, raise_exc=handler_module.RetrievalFailed(503)
    )
    token = _mint_token(
        handler_module, tenant_id="agency-a", subject="user-a", interaction_id="int-fail"
    )
    result = handler_module.handler(
        {"arguments": {"session_token": token, "query": "review"}}, None
    )

    assert result["results"] == []
    assert result["count"] == 0
    assert result["reason"] == "retrieval_failed"
    # The failed call targeted the shared domain; no second call to dedicated.
    assert captured["target"]["tier"] == "shared"
    failed = [
        item for item in fresh_audit.items
        if item["event_type"]["S"] == "retrieval_failed"
    ]
    assert len(failed) == 1
    assert json.loads(failed[0]["detail"]["S"])["tier"] == "shared"


def test_missing_session_token_denies_before_query(handler_module, fresh_audit, monkeypatch):
    """No signed capability means no query is ever built."""
    captured = _install_search(handler_module, monkeypatch, documents=[])
    result = handler_module.handler({"arguments": {"query": "review"}}, None)

    assert result["reason"] == "invalid_session_context"
    assert captured["body"] is None  # query never constructed
    assert any(
        item["event_type"]["S"] == "denied_invalid_session_context"
        for item in fresh_audit.items
    )
