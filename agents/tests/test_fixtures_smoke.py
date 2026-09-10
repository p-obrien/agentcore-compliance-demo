"""Smoke tests proving the shared fixtures load and behave as documented.

These are not correctness properties; they exist so ``pytest`` collects a real
test and confirms the mocked-boundary fixtures import cleanly with no live AWS
dependency. The 18 correctness property tests arrive in later tasks.
"""

from __future__ import annotations

import jsonschema
import pytest


def test_jwt_key_retriever_returns_generated_keys(jwt_signing_keys, jwt_key_retriever):
    key = jwt_signing_keys[0]
    resolved = jwt_key_retriever.get(key.kid)
    assert resolved is key
    assert jwt_key_retriever.get("unknown-kid") is None
    assert jwt_key_retriever.calls == [key.kid, "unknown-kid"]


def test_opensearch_http_replays_scripted_then_default(opensearch_http):
    response_cls = type(opensearch_http.default_response)
    opensearch_http.queue_response(
        "POST", "/permits/_search", response_cls(status_code=403, json_body={"denied": True})
    )
    denied = opensearch_http.request("POST", "/permits/_search", body={"query": {}})
    assert denied.status_code == 403
    fallback = opensearch_http.request("POST", "/permits/_search", body={"query": {}})
    assert fallback.status_code == 200
    assert len(opensearch_http.requests) == 2


def test_audit_repository_is_append_only(dynamodb_repositories):
    audit = dynamodb_repositories["audit"]
    audit.put({"event_time_ns": 1, "action": "interaction_started"})
    with pytest.raises(audit.AppendOnlyViolation):
        audit.put({"event_time_ns": 1, "action": "tampered"})
    with pytest.raises(audit.AppendOnlyViolation):
        audit.update(1, {"action": "tampered"})


def test_staging_conditional_put(dynamodb_repositories):
    staging = dynamodb_repositories["staging"]
    staging.put({"approval_id": "a-1", "status": "PENDING_APPROVAL"})
    with pytest.raises(staging.ConditionalCheckFailed):
        staging.put(
            {"approval_id": "a-1", "status": "PENDING_APPROVAL"},
            condition=lambda existing: existing is None,
        )
    assert staging.query(status="PENDING_APPROVAL") == [
        {"approval_id": "a-1", "status": "PENDING_APPROVAL"}
    ]


def test_step_functions_callbacks_record_tokens(step_functions_callbacks):
    step_functions_callbacks.send_task_success("token-1", {"status": "APPROVED"})
    step_functions_callbacks.send_task_failure("token-2", error="CommitFailed")
    assert step_functions_callbacks.resumed_tokens == {"token-1", "token-2"}


def test_fake_clock_records_sleeps_without_blocking(fake_clock):
    start = fake_clock.now()
    fake_clock.sleep(2.5)
    fake_clock.sleep(5.0)
    assert fake_clock.slept == [2.5, 5.0]
    assert fake_clock.now() == start + 7.5


def test_apply_guardrail_helpers(apply_guardrail):
    apply_guardrail.queue(apply_guardrail.masked_response("PID-******"))
    apply_guardrail.queue(apply_guardrail.grounding_response(None))
    masked = apply_guardrail.apply_guardrail(source="INPUT", content="PID-482913")
    assert masked["action"] == "GUARDRAIL_INTERVENED"
    absent = apply_guardrail.apply_guardrail(source="OUTPUT", content="draft")
    assert absent["groundingScore"] is None
    assert [c["source"] for c in apply_guardrail.calls] == ["INPUT", "OUTPUT"]


def test_jsonschema_dependency_is_available():
    schema = {"type": "object", "required": ["permit_id"], "properties": {"permit_id": {"type": "string"}}}
    jsonschema.validate({"permit_id": "A-1001"}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({}, schema)


def test_interaction_id_factory_is_unique(interaction_id_factory):
    ids = {interaction_id_factory() for _ in range(5)}
    assert len(ids) == 5
