"""Thin wrapper around AgentCore ``InvokeAgentRuntime`` for the demo web API.

The browser cannot call ``bedrock-agentcore:InvokeAgentRuntime`` directly: it is
a SigV4-signed AWS API with no CORS surface, and the demo assessors are Cognito
identities with no AWS credentials. This module runs inside the demo-api Lambda,
which holds the invoke permission, and forwards the caller's two Cognito tokens
unchanged so the runtime derives tenant from the verified claims exactly as it
does for any other client.

It holds no tenant logic. It relays ``Authorization`` and ``X-Id-Token``, reads
the buffered response stream, and separates streamed model text (the draft) from
the final ``meta`` object the assessment agent emits.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
ASSESSMENT_RUNTIME_ARN = os.environ.get("ASSESSMENT_RUNTIME_ARN", "")


class RuntimeInvocationError(RuntimeError):
    """Raised when the runtime call fails or returns no usable response."""


@dataclass
class AssessmentResult:
    """Buffered outcome of one assessment invocation.

    ``draft`` is the concatenated model text. ``meta`` is the final metadata
    object the agent yields (interaction_id, grounding_score, review_reason,
    approval_execution_arn, ...). ``error`` carries an agent-reported error such
    as an identity denial, which is a demonstrable outcome rather than a fault.
    """

    draft: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    events: list[Any] = field(default_factory=list)


def _event_text(event: Any) -> str:
    """Extract streamed model text from a yielded event.

    Mirrors the extraction the assessment agent uses so the reconstructed draft
    matches what the model produced, without trusting any single event shape.
    """
    if isinstance(event, str):
        return event
    if not isinstance(event, dict):
        return ""
    parts: list[str] = []
    for key in ("text", "delta", "data", "content"):
        value = event.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            parts.append(_event_text(value))
        elif isinstance(value, list):
            parts.extend(_event_text(item) for item in value)
    return "".join(parts)


def _iter_json_objects(raw: str):
    """Yield parsed objects from the runtime response body.

    AgentCore serializes the agent's yielded events. Depending on transport the
    buffered body is either a JSON array, newline-delimited JSON, or SSE
    ``data:`` lines. Handle all three so the demo does not depend on one
    transport encoding.
    """
    text = raw.strip()
    if not text:
        return
    # Whole-body JSON (array or single object).
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            yield from parsed
        else:
            yield parsed
        return
    except json.JSONDecodeError:
        pass
    # Line-delimited or SSE.
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[len("data:") :].strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            # A bare text chunk that is not JSON is still draft content.
            yield line


def _invocations_url() -> str:
    """Build the AgentCore Runtime data-plane invocations URL for the ARN.

    A CUSTOM_JWT runtime is invoked over HTTPS with an ``Authorization: Bearer``
    header, not through the SigV4 ``bedrock-agentcore:InvokeAgentRuntime`` SDK
    call. The SDK's InvokeAgentRuntime input has no member for the caller's
    bearer token or the allowlisted ``X-Id-Token``, so a JWT-authorized runtime
    must be reached at its invocations endpoint. The ARN is percent-encoded into
    the path exactly as the AWS documented client does.
    """
    encoded_arn = ASSESSMENT_RUNTIME_ARN.replace(":", "%3A").replace("/", "%2F")
    return (
        f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/"
        f"{encoded_arn}/invocations?qualifier=DEFAULT"
    )


def invoke_assessment(*, access_authorization: str, id_token: str, prompt: str, permit_id: str | None, runtime_session_id: str) -> AssessmentResult:
    """Invoke the assessment runtime, forwarding both Cognito tokens.

    ``access_authorization`` is the full ``Authorization`` header value
    (``Bearer <access token>``). ``id_token`` is the companion ID token the
    runtime requires as ``X-Id-Token``. Tenant scope is decided inside the
    runtime and retrieval Lambda from these tokens; this wrapper adds nothing.

    The runtime uses a CUSTOM_JWT authorizer, so it is invoked over its HTTPS
    invocations URL with the bearer token, not through the SigV4 SDK call.
    """
    if not ASSESSMENT_RUNTIME_ARN:
        raise RuntimeInvocationError("assessment runtime ARN is not configured")

    payload = {"prompt": prompt}
    if permit_id:
        payload["context"] = {"permit_id": permit_id}

    # AgentCore requires a runtime session id of at least 33 characters. A
    # Cognito subject (a UUID) satisfies this; pad defensively if a shorter id
    # is ever passed so the runtime does not reject the request.
    session_id = runtime_session_id or ""
    if len(session_id) < 33:
        session_id = (session_id + "-" + "0" * 33)[:48]

    # AgentCore consumes the inbound Authorization header for its authorizer and
    # does not forward it to the agent. Send the raw access token as
    # X-Access-Token (allowlisted and forwarded) so the agent can verify the
    # subject and authenticate the Gateway call. Keep Authorization too: it is
    # what the runtime's CUSTOM_JWT authorizer validates to admit the request.
    bearer_prefix = "bearer "
    raw_access = access_authorization
    if access_authorization.lower().startswith(bearer_prefix):
        raw_access = access_authorization[len(bearer_prefix):].strip()

    request = urllib.request.Request(
        _invocations_url(),
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": access_authorization,
            "X-Access-Token": raw_access,
            "X-Id-Token": id_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as http_response:
            raw = http_response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else ""
        raise RuntimeInvocationError(f"HTTP {exc.code} from runtime: {detail[:500]}") from exc
    except Exception as exc:  # transport failures
        raise RuntimeInvocationError(str(exc)) from exc

    result = AssessmentResult()
    draft_parts: list[str] = []
    for event in _iter_json_objects(raw):
        result.events.append(event)
        if isinstance(event, dict):
            if "meta" in event and isinstance(event["meta"], dict):
                result.meta = event["meta"]
                continue
            if "error" in event and isinstance(event["error"], str):
                result.error = event["error"]
                if isinstance(event.get("tenant_id"), str):
                    result.meta.setdefault("tenant_id", event["tenant_id"])
                if isinstance(event.get("interaction_id"), str):
                    result.meta.setdefault("interaction_id", event["interaction_id"])
                if isinstance(event.get("review_reason"), str):
                    result.meta.setdefault("review_reason", event["review_reason"])
                continue
        draft_parts.append(_event_text(event))

    result.draft = "".join(draft_parts).strip()
    return result
