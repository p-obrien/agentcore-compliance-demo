"""Smoke tests proving the seed fixtures load and behave as documented.

These confirm ``pytest`` collects a real test and that the mocked OpenSearch HTTP
and clock/sleep boundaries import cleanly with no live AWS dependency. The seed
routing and bounded-retry property tests (Properties 16 and 17) arrive in a
later task.
"""

from __future__ import annotations

import jsonschema
import pytest


def test_opensearch_http_replays_retry_sequence(opensearch_http):
    # 503 twice, then 200 — the shape a bounded-retry test drives.
    opensearch_http.queue_sequence("PUT", "/permits/_doc/A-1001", [503, 503, 200])
    statuses = [
        opensearch_http.request("PUT", "/permits/_doc/A-1001", body={"permit_id": "A-1001"}).status_code
        for _ in range(3)
    ]
    assert statuses == [503, 503, 200]
    assert opensearch_http.attempts("PUT", "/permits/_doc/A-1001") == 3


def test_opensearch_http_falls_back_to_default(opensearch_http):
    resp = opensearch_http.request("PUT", "/permits/_doc/C-3001", body={"permit_id": "C-3001"})
    assert resp.status_code == 200
    assert resp.json() == {"result": "updated"}


def test_fake_clock_records_backoff_without_blocking(fake_clock):
    start = fake_clock.now()
    for delay in (0.5, 1.0, 2.0):
        fake_clock.sleep(delay)
    assert fake_clock.slept == [0.5, 1.0, 2.0]
    assert fake_clock.now() == start + 3.5


def test_jsonschema_dependency_is_available():
    schema = {
        "type": "object",
        "required": ["permit_id", "tenant_id"],
        "properties": {"permit_id": {"type": "string"}, "tenant_id": {"type": "string"}},
    }
    jsonschema.validate({"permit_id": "A-1001", "tenant_id": "agency-a"}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"permit_id": "A-1001"}, schema)
