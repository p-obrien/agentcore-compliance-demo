"""Lambda entry point for the in-VPC OpenSearch seed runner."""

from __future__ import annotations

import json
from typing import Any

import load_seed


def handler(event: dict[str, Any] | None, _context: Any) -> dict[str, Any]:
    """Seed the managed domains from a private Lambda invocation.

    The function has no direct OpenSearch write grant. ``load_seed`` assumes the
    separate seed role before signing every managed-domain request.
    """
    event = event or {}
    if not isinstance(event, dict):
        raise ValueError("seed invocation payload must be a JSON object")

    result = load_seed.run(no_embeddings=bool(event.get("no_embeddings", False)))
    return {
        "statusCode": 200,
        "body": json.dumps(result, separators=(",", ":")),
    }
