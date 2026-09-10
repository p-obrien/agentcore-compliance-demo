"""Independent guardrail evaluation for deterministic approval routing.

`ApplyGuardrail` runs independently of model invocation. This module calls it
twice per interaction:

- ``evaluate_input`` runs before the model request. It masks configured fake PII
  (the ``PID-######`` value) so the raw token never reaches the model or the
  trace, and it blocks indirect-injection content so a poisoned document cannot
  become a model instruction.
- ``evaluate_grounding`` runs on the drafted output. It returns the actual
  contextual grounding score (a number or ``None``) plus a review reason. A
  missing or low score is never treated as a pass; the caller still routes to
  human review.

The response-parsing logic is separated from the AWS call so the property tests
can exercise it against scripted ``ApplyGuardrail`` responses with no live call.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable

import boto3

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")

# The synthetic PII token pattern used throughout the demo documents. Guardrail
# PII masking is the primary control; this pattern is a defensive fallback so a
# raw value never reaches the trace even if the guardrail response omits a
# masked output (e.g. guardrail not configured in a local run).
FAKE_PII_PATTERN = re.compile(r"PID-\d{6}")
_PII_PLACEHOLDER = "{PII}"

_guardrails = boto3.client("bedrock-runtime", region_name=REGION)


@dataclass(frozen=True)
class InputGuardrailResult:
    """Outcome of the input ``ApplyGuardrail`` pass.

    ``blocked`` is ``True`` when the guardrail intervened on an indirect-injection
    or disallowed-topic policy, meaning the content must not become model input.
    ``sanitized_text`` is the masked text safe to send to the model and to record
    in the trace; it never contains the raw fake PII token. ``reason`` is a stable
    machine label for the audit/trace, never a raw token or response body.
    """

    blocked: bool
    sanitized_text: str
    reason: str


def _mask_fake_pii(text: str) -> str:
    """Replace any residual fake PII token with a placeholder.

    Applied to whatever text will be recorded or forwarded, so the raw
    ``PID-######`` value is excluded from the model request and the trace even
    when the guardrail response does not carry a masked output.
    """
    return FAKE_PII_PATTERN.sub(_PII_PLACEHOLDER, text)


def parse_input_response(response: dict[str, Any], original_text: str) -> InputGuardrailResult:
    """Interpret an input ``ApplyGuardrail`` response without any AWS call.

    A guardrail intervention on a topic or prompt-attack policy blocks the
    content. Otherwise the masked output (if any) is adopted, then a defensive
    fake-PII mask is applied so the raw token cannot survive.
    """
    action = response.get("action")
    assessments = response.get("assessments", []) or []

    def _has_policy(name: str) -> bool:
        return any(name in assessment for assessment in assessments)

    if action == "GUARDRAIL_INTERVENED" and (
        _has_policy("topicPolicy") or _has_policy("wordPolicy")
    ):
        # Indirect-injection / disallowed content: never use as model input.
        return InputGuardrailResult(
            blocked=True, sanitized_text="", reason="guardrail_input_blocked"
        )

    masked = None
    for output in response.get("outputs", []) or []:
        if isinstance(output, dict) and isinstance(output.get("text"), str):
            masked = output["text"]
            break

    sanitized = _mask_fake_pii(masked if masked is not None else original_text)
    reason = "input_pii_masked" if masked is not None else "input_clean"
    return InputGuardrailResult(blocked=False, sanitized_text=sanitized, reason=reason)


def evaluate_input(
    *, text: str, _client: Callable[..., dict[str, Any]] | None = None
) -> InputGuardrailResult:
    """Run the input ``ApplyGuardrail`` pass and interpret the result.

    ``_client`` is injectable for tests; production uses the module Bedrock
    client. When the guardrail is not configured, the fake-PII mask is still
    applied so a local or misconfigured run never forwards a raw token.
    """
    if not GUARDRAIL_ID:
        return InputGuardrailResult(
            blocked=False, sanitized_text=_mask_fake_pii(text), reason="guardrail_not_configured"
        )
    call = _client or _apply_guardrail
    try:
        response = call(
            guardrailIdentifier=GUARDRAIL_ID,
            guardrailVersion=GUARDRAIL_VERSION,
            source="INPUT",
            content=[{"text": {"text": text[:50000]}}],
        )
    except Exception as exc:  # A failed input guard blocks rather than passes raw text.
        return InputGuardrailResult(
            blocked=True,
            sanitized_text="",
            reason=f"input_guardrail_failed:{type(exc).__name__}",
        )
    return parse_input_response(response, text)


def parse_grounding_response(response: dict[str, Any]) -> tuple[float | None, str]:
    """Interpret an output ``ApplyGuardrail`` response without any AWS call.

    Returns the contextual grounding score (or ``None`` when absent) and a stable
    review reason. A blocked or detected grounding filter routes to review; an
    absent score also routes to review, never a pass.
    """
    for assessment in response.get("assessments", []) or []:
        filters = assessment.get("contextualGroundingPolicy", {}).get("filters", [])
        for result in filters:
            if result.get("type") == "GROUNDING" and isinstance(
                result.get("score"), (int, float)
            ):
                score = float(result["score"])
                if result.get("action") == "BLOCKED" or result.get("detected"):
                    return score, "grounding_below_guardrail_threshold"
                return score, "grounding_verified"
    return None, "grounding_score_unavailable"


def evaluate_grounding(
    *,
    source: str,
    query: str,
    draft: str,
    _client: Callable[..., dict[str, Any]] | None = None,
) -> tuple[float | None, str]:
    """Return the actual grounding score or a fail-safe review reason.

    A missing score is never treated as a passing result. The caller still
    creates a human approval, with the reason making the operational gap clear.
    """
    if not GUARDRAIL_ID:
        return None, "guardrail_not_configured"
    if not draft.strip():
        return None, "empty_assessment_draft"
    call = _client or _apply_guardrail
    try:
        response = call(
            guardrailIdentifier=GUARDRAIL_ID,
            guardrailVersion=GUARDRAIL_VERSION,
            source="OUTPUT",
            outputScope="FULL",
            content=[
                {"text": {"text": source[:50000], "qualifiers": ["grounding_source"]}},
                {"text": {"text": query[:10000], "qualifiers": ["query"]}},
                {"text": {"text": draft[:50000], "qualifiers": ["guard_content"]}},
            ],
        )
    except Exception as exc:  # Guardrail failures must route to review, never write back.
        return None, f"grounding_evaluation_failed:{type(exc).__name__}"
    return parse_grounding_response(response)


def _apply_guardrail(**kwargs: Any) -> dict[str, Any]:
    return _guardrails.apply_guardrail(**kwargs)
