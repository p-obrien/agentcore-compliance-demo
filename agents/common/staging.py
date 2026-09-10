"""Schema-gated, idempotent proposal staging.

Agent extraction output is validated against a strict JSON schema before any
proposal is created. A schema-invalid extraction produces neither a
Pending_Proposal nor a Mock_Record_Store record. A schema-valid extraction
produces exactly one Pending_Proposal in ``PENDING_APPROVAL``, keyed by the
Interaction_ID so a repeated evaluation (low or absent grounding, guardrail
failure) retains the existing proposal instead of creating a second one.

The record store is never written here: staging only ever touches the
Staging_Table, and the write is a conditional create. Validation and proposal
construction are pure functions the property tests drive with an in-memory
repository and no live AWS call.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import jsonschema

PENDING_APPROVAL = "PENDING_APPROVAL"

# Strict extraction schema. ``additionalProperties: false`` rejects any field the
# model invents; every listed field is required so a partial extraction is
# treated as invalid rather than silently staged.
EXTRACTION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["permit_id", "permit_type", "applicant", "status", "summary"],
    "properties": {
        "permit_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "permit_type": {"type": "string", "minLength": 1, "maxLength": 128},
        "applicant": {"type": "string", "minLength": 1, "maxLength": 256},
        "status": {"type": "string", "minLength": 1, "maxLength": 64},
        "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
    },
}

_validator = jsonschema.Draft202012Validator(EXTRACTION_SCHEMA)


class SchemaInvalid(ValueError):
    """Raised when agent extraction output does not satisfy the strict schema."""


@dataclass(frozen=True)
class StagingResult:
    """Outcome of one staging attempt.

    ``created`` is ``True`` only when this call wrote a new proposal; a repeat
    for the same Interaction_ID returns ``created=False`` with the existing
    proposal so the caller never stages a duplicate.
    """

    proposal_id: str
    created: bool
    proposal: dict[str, Any]


def validate_extraction(extraction: Any) -> None:
    """Validate against the strict schema, raising ``SchemaInvalid`` on failure."""
    if not isinstance(extraction, dict):
        raise SchemaInvalid("extraction must be a JSON object")
    errors = sorted(_validator.iter_errors(extraction), key=lambda e: e.path)
    if errors:
        # Report the first field-level reason; never echo the whole payload.
        first = errors[0]
        location = "/".join(str(p) for p in first.path) or "<root>"
        raise SchemaInvalid(f"schema violation at {location}: {first.message}")


def build_proposal(
    *,
    interaction_id: str,
    tenant_id: str,
    extraction: dict[str, Any],
    subject: str,
    review_reason: str,
    grounding_score: float | None,
    now_ns: int | None = None,
) -> dict[str, Any]:
    """Build one Pending_Proposal keyed deterministically by Interaction_ID.

    ``approval_id`` equals the Interaction_ID, so a repeat evaluation produces
    the same key and the conditional create in :func:`stage_proposal` collapses
    to a no-op instead of a second proposal.
    """
    return {
        "approval_id": interaction_id,
        "interaction_id": interaction_id,
        "tenant_id": tenant_id,
        "status": PENDING_APPROVAL,
        "verified_subject": subject,
        "review_reason": review_reason,
        "grounding_score": grounding_score,
        "extraction": extraction,
        "created_at_ns": now_ns if now_ns is not None else time.time_ns(),
    }


def stage_proposal(
    *,
    repository: Any,
    interaction_id: str,
    tenant_id: str,
    extraction: Any,
    subject: str,
    review_reason: str,
    grounding_score: float | None,
    now_ns: int | None = None,
) -> StagingResult:
    """Validate, then idempotently stage exactly one Pending_Proposal.

    Raises ``SchemaInvalid`` before any write when the extraction is invalid, so
    no proposal and no record are created. On valid input, attempts a conditional
    create keyed by Interaction_ID; a repeat returns the existing proposal with
    ``created=False``.

    ``repository`` must expose ``put(item, condition=...)`` raising
    ``ConditionalCheckFailed`` on a key collision and ``get(key)`` returning the
    stored item (the in-memory ``FakeDynamoDBRepository`` and a thin DynamoDB
    wrapper both satisfy this).
    """
    validate_extraction(extraction)
    proposal = build_proposal(
        interaction_id=interaction_id,
        tenant_id=tenant_id,
        extraction=extraction,
        subject=subject,
        review_reason=review_reason,
        grounding_score=grounding_score,
        now_ns=now_ns,
    )
    try:
        repository.put(proposal, condition=lambda existing: existing is None)
    except getattr(repository, "ConditionalCheckFailed", _NoCollision):
        existing = repository.get(interaction_id)
        return StagingResult(proposal_id=interaction_id, created=False, proposal=existing)
    return StagingResult(proposal_id=interaction_id, created=True, proposal=proposal)


class _NoCollision(Exception):
    """Fallback exception type when a repository declares no ConditionalCheckFailed."""
