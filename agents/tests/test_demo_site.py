"""Unit tests for the demo web API Lambda (modules/demo_site/src/api).

The demo-api Lambda relays a signed-in assessor's Cognito tokens to the
assessment runtime and exposes a tenant-scoped read of the audit trail. These
tests load the three sibling modules by path and drive the handler directly,
stubbing the AgentCore runtime and DynamoDB boundaries. No live AWS call is made.

What is proven here:
  - /assess forwards both tokens and returns the buffered draft + meta.
  - /assess rejects a request with no X-Id-Token before any runtime call.
  - A runtime failure maps to HTTP 502, not a 200 with empty content.
  - Audit reads are scoped to the caller's verified tenant: an Agency A caller
    cannot read Agency B rows even with a supplied interaction_id.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_API_SRC = (
    Path(__file__).resolve().parents[1] / ".." / "modules" / "demo_site" / "src" / "api"
).resolve()


def _load(module_name: str, file_name: str):
    spec = importlib.util.spec_from_file_location(module_name, _API_SRC / file_name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture()
def api(monkeypatch):
    """Load assessment_runtime, audit_read, and handler with stubbed AWS clients."""
    runtime_client = _load("demo_assessment_runtime", "assessment_runtime.py")
    audit_read = _load("demo_audit_read", "audit_read.py")

    # The handler imports `assessment_runtime` and `audit_read` by bare name;
    # alias the path-loaded modules under those names so the import resolves.
    # The module is deliberately NOT named `runtime_client`: that name collides
    # with the Lambda Runtime Interface Client's compiled module in the base
    # image, which shadowed our source at runtime and 500'd every /assess call.
    sys.modules["assessment_runtime"] = runtime_client
    sys.modules["audit_read"] = audit_read
    handler = _load("demo_handler", "handler.py")

    yield handler, runtime_client, audit_read

    for name in (
        "demo_assessment_runtime",
        "demo_audit_read",
        "demo_handler",
        "assessment_runtime",
        "audit_read",
    ):
        sys.modules.pop(name, None)


def _event(method, path, *, groups=("agency-a-assessors",), sub="assessor-a", headers=None, body=None, query=None):
    return {
        "rawPath": path,
        "requestContext": {
            "http": {"method": method},
            "authorizer": {"jwt": {"claims": {"sub": sub, "cognito:groups": list(groups)}}},
        },
        "headers": headers or {},
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
    }


class _FakeRuntime:
    """Records the invoke call and replays a scripted response body.

    The runtime is now invoked over its HTTPS invocations URL (a CUSTOM_JWT
    runtime cannot be reached through the SigV4 SDK call, whose input has no
    slot for the caller's bearer token). The stub replaces
    ``assessment_runtime.urllib.request.urlopen`` and records the outgoing
    ``urllib.request.Request`` so tests can assert the forwarded headers.
    """

    def __init__(self, body, *, raise_exc=None):
        self.body = body
        self.raise_exc = raise_exc
        self.calls = []  # list of urllib.request.Request objects

    def install(self, module):
        def fake_urlopen(request, timeout=None):
            self.calls.append(request)
            if self.raise_exc is not None:
                raise self.raise_exc

            body = self.body

            class _Resp:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *a):
                    return False

                def read(self_inner):
                    return body.encode("utf-8") if isinstance(body, str) else body

            return _Resp()

        module.urllib.request.urlopen = fake_urlopen


def test_assess_forwards_tokens_and_buffers_draft(api):
    handler, runtime_client, _audit = api
    # Agent yields streamed text events, then a final meta object.
    stream = json.dumps(
        [
            {"text": "Draft "},
            {"text": "assessment."},
            {"meta": {"tenant_id": "agency-a", "interaction_id": "int-1", "grounding_score": 0.91, "review_reason": "ok", "approval_execution_arn": "arn:sfn:exec"}},
        ]
    )
    fake = _FakeRuntime(stream)
    fake.install(runtime_client)
    runtime_client.ASSESSMENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:ap-southeast-2:1:runtime/assess"

    event = _event(
        "POST",
        "/assess",
        headers={"authorization": "Bearer access-token-value", "x-id-token": "id-token-value"},
        body={"prompt": "Draft an assessment for A-1001", "permit_id": "A-1001"},
    )
    resp = handler.handler(event, None)
    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])
    assert payload["verified_tenant"] == "agency-a"
    assert payload["draft"] == "Draft assessment."
    assert payload["meta"]["interaction_id"] == "int-1"
    assert payload["meta"]["grounding_score"] == 0.91

    # Both tokens were forwarded verbatim as runtime request headers. urllib
    # normalizes header keys to title-case, so match on that form.
    request = fake.calls[0]
    assert request.get_header("Authorization") == "Bearer access-token-value"
    assert request.get_header("X-id-token") == "id-token-value"
    # Reached the runtime's invocations URL, not a SigV4 SDK endpoint.
    assert "/runtimes/" in request.full_url and "/invocations" in request.full_url


def test_assess_requires_id_token(api):
    handler, runtime_client, _audit = api
    fake = _FakeRuntime("[]")
    fake.install(runtime_client)
    runtime_client.ASSESSMENT_RUNTIME_ARN = "arn:runtime"

    event = _event(
        "POST",
        "/assess",
        headers={"authorization": "Bearer access-token-value"},  # no X-Id-Token
        body={"prompt": "hello"},
    )
    resp = handler.handler(event, None)
    assert resp["statusCode"] == 400
    assert "X-Id-Token" in json.loads(resp["body"])["error"]
    # The runtime was never called.
    assert fake.calls == []


def test_assess_runtime_failure_maps_to_502(api):
    handler, runtime_client, _audit = api
    fake = _FakeRuntime("", raise_exc=RuntimeError("boom"))
    fake.install(runtime_client)
    runtime_client.ASSESSMENT_RUNTIME_ARN = "arn:runtime"

    event = _event(
        "POST",
        "/assess",
        headers={"authorization": "Bearer a", "x-id-token": "i"},
        body={"prompt": "hello"},
    )
    resp = handler.handler(event, None)
    assert resp["statusCode"] == 502


def test_assess_without_assessor_group_is_forbidden(api):
    handler, _runtime, _audit = api
    event = _event(
        "POST",
        "/assess",
        groups=(),  # no assessor group
        headers={"authorization": "Bearer a", "x-id-token": "i"},
        body={"prompt": "hello"},
    )
    resp = handler.handler(event, None)
    assert resp["statusCode"] == 403


@pytest.mark.parametrize(
    "raw_groups",
    [
        "[agency-a-assessors]",  # HTTP API flattens a single-value claim
        "[agency-a-assessors agency-a-approvers]",  # multi-valued, space-separated, unquoted
        '["agency-a-assessors"]',  # JSON array string
        "agency-a-assessors",  # plain string
    ],
)
def test_assess_resolves_tenant_from_flattened_group_claim(api, raw_groups):
    """The API Gateway HTTP API authorizer passes cognito:groups as a string.

    A single value arrives as '[agency-a-assessors]' and multiple values as
    '[a b]' (unquoted, space-separated). The earlier json.loads-on-'[' parser
    returned [] for those, so every request 403'd. Feed the raw string the
    authorizer actually produces and assert the tenant resolves.
    """
    handler, runtime_client, _audit = api
    stream = json.dumps(
        [
            {"text": "ok"},
            {"meta": {"tenant_id": "agency-a", "interaction_id": "int-9"}},
        ]
    )
    fake = _FakeRuntime(stream)
    fake.install(runtime_client)
    runtime_client.ASSESSMENT_RUNTIME_ARN = "arn:runtime"

    event = {
        "rawPath": "/assess",
        "requestContext": {
            "http": {"method": "POST"},
            "authorizer": {"jwt": {"claims": {"sub": "assessor-a", "cognito:groups": raw_groups}}},
        },
        "headers": {"authorization": "Bearer a", "x-id-token": "i"},
        "body": json.dumps({"prompt": "hello"}),
    }
    resp = handler.handler(event, None)
    assert resp["statusCode"] == 200, resp["body"]
    assert json.loads(resp["body"])["verified_tenant"] == "agency-a"


class _FakeDdb:
    """Minimal DynamoDB stub returning scripted Items per query."""

    def __init__(self, items):
        self.items = items
        self.queries = []

    def query(self, **kwargs):
        self.queries.append(kwargs)
        return {"Items": self.items}


def _audit_item(interaction_id, tenant_id, action, outcome="passed"):
    return {
        "interaction_id": {"S": interaction_id},
        "ts": {"N": "1"},
        "tenant_id": {"S": tenant_id},
        "action": {"S": action},
        "outcome": {"S": outcome},
        "actor_type": {"S": "agent"},
        "actor_id": {"S": "assessment-agent"},
        "detail": {"S": json.dumps({"k": "v"})},
    }


def test_audit_interaction_filters_cross_tenant_rows(api):
    handler, _runtime, audit_read = api
    # The interaction returns rows for two tenants; an Agency A caller must see
    # only Agency A rows even though it supplied the interaction_id.
    audit_read._client = _FakeDdb(
        [
            _audit_item("int-x", "agency-a", "retrieval"),
            _audit_item("int-x", "agency-b", "retrieval"),
        ]
    )
    audit_read.AUDIT_TABLE = "audit"

    event = _event(
        "GET",
        "/audit",
        groups=("agency-a-assessors",),
        query={"interaction_id": "int-x"},
    )
    resp = handler.handler(event, None)
    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])
    assert payload["tenant_id"] == "agency-a"
    assert [e["tenant_id"] for e in payload["events"]] == ["agency-a"]
    assert payload["events"][0]["action"] == "retrieval"


def test_audit_recent_scopes_query_to_caller_tenant(api):
    handler, _runtime, audit_read = api
    ddb = _FakeDdb([_audit_item("int-y", "agency-b", "assessment_review_requested")])
    audit_read._client = ddb
    audit_read.AUDIT_TABLE = "audit"

    event = _event("GET", "/audit/recent", groups=("agency-b-assessors",), query={"limit": "5"})
    resp = handler.handler(event, None)
    assert resp["statusCode"] == 200
    # The GSI query is keyed on the verified tenant, not a request field.
    q = ddb.queries[0]
    assert q["IndexName"] == audit_read.BY_TENANT_INDEX
    assert q["ExpressionAttributeValues"][":t"]["S"] == "agency-b"
    assert q["Limit"] == 5


def test_assess_surfaces_agent_error_as_demonstrable_outcome(api):
    handler, runtime_client, _audit = api
    # An identity denial from the agent is a valid demo outcome, not a 5xx.
    stream = json.dumps([{"error": "identity has no authorised tenant assessor group", "tenant_id": None}])
    fake = _FakeRuntime(stream)
    fake.install(runtime_client)
    runtime_client.ASSESSMENT_RUNTIME_ARN = "arn:runtime"

    event = _event(
        "POST",
        "/assess",
        headers={"authorization": "Bearer a", "x-id-token": "i"},
        body={"prompt": "hello"},
    )
    resp = handler.handler(event, None)
    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])
    assert payload["agent_error"] == "identity has no authorised tenant assessor group"
    assert payload["draft"] == ""
