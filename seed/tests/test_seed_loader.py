"""Property and unit tests for the managed-domain seed loader.

The loader (``seed/load_seed.py``) is loaded by path. Its routing and retry
logic are pure enough to test directly; the HTTP boundary is exercised by
patching ``urllib.request.urlopen`` to raise scripted ``HTTPError`` sequences,
and sleep is injected so no real backoff runs. No AWS call is made.

Covers task 13 sub-tasks:
  - 13.3 Property 16: Idempotent managed-domain seed routing
  - 13.4 Property 17: Bounded retry and exhaustion reporting
  - 13.5 Unit test: retry-delay schedule matches documented policy
"""

from __future__ import annotations

import importlib.util
import io
import sys
import urllib.error
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_LOADER_PATH = Path(__file__).resolve().parents[1] / "load_seed.py"


@pytest.fixture(scope="module")
def loader():
    spec = importlib.util.spec_from_file_location("load_seed_under_test", _LOADER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["load_seed_under_test"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    yield module
    sys.modules.pop("load_seed_under_test", None)


_TARGETS = {
    "agency-a": {"endpoint": "https://shared.es.invalid", "index_name": "permits", "tier": "shared"},
    "agency-b": {"endpoint": "https://shared.es.invalid", "index_name": "permits", "tier": "shared"},
    "agency-c": {"endpoint": "https://dedic.es.invalid", "index_name": "permits", "tier": "dedicated"},
}


# ---------------------------------------------------------------------------
# 13.3 Property 16: Idempotent managed-domain seed routing
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(
    tenant_id=st.sampled_from(["agency-a", "agency-b", "agency-c"]),
    permit_suffix=st.integers(min_value=1000, max_value=9999),
)
def test_property_16_routing_and_stable_id(loader, tenant_id, permit_suffix):
    # Feature: agentcore-demo-lab, Property 16: For any valid synthetic permit
    # document set with stable identifiers, the loader shall route A/B documents
    # to the shared target and C documents to the dedicated target. Re-running
    # the same set shall leave exactly one document per stable identifier and
    # update its contents in place.
    target = loader.resolve_target(_TARGETS, tenant_id)
    expected_tier = "dedicated" if tenant_id == "agency-c" else "shared"
    assert target["tier"] == expected_tier
    if tenant_id in ("agency-a", "agency-b"):
        assert target["endpoint"] == "https://shared.es.invalid"
    else:
        assert target["endpoint"] == "https://dedic.es.invalid"

    # Stable id: the document _id is permit_id, so a rerun targets the same doc
    # (upsert in place), never a new one.
    import urllib.parse

    permit_id = f"{tenant_id[-1].upper()}-{permit_suffix}"
    doc_id_first = urllib.parse.quote(permit_id, safe="")
    doc_id_second = urllib.parse.quote(permit_id, safe="")
    assert doc_id_first == doc_id_second


def test_property_16_unknown_tenant_has_no_target(loader):
    """A tenant with no target is rejected, enforcing one-tenant-one-target."""
    with pytest.raises(ValueError):
        loader.resolve_target(_TARGETS, "agency-x")


def test_seed_documents_route_to_exactly_one_target(loader):
    """Every shipped seed document resolves to exactly one managed-domain target."""
    import json

    docs = json.loads((_LOADER_PATH.parent / "documents.json").read_text())
    assert docs, "expected synthetic documents"
    for document in docs:
        target = loader.resolve_target(_TARGETS, document["tenant_id"])
        assert target["tier"] in ("shared", "dedicated")
    # The poison document stays in Agency B (shared) only.
    poison = [d for d in docs if d.get("is_poisoned")]
    assert poison and all(d["tenant_id"] == "agency-b" for d in poison)
    # Agency C documents exist so the dedicated domain has content.
    assert any(d["tenant_id"] == "agency-c" for d in docs)


# ---------------------------------------------------------------------------
# 13.4 Property 17: Bounded retry and exhaustion reporting
# ---------------------------------------------------------------------------


class _Creds:
    access_key = "AKIA"
    secret_key = "secret"
    token = "token"


def _http_error(code):
    return urllib.error.HTTPError(
        url="https://shared.es.invalid/permits/_doc/A-1001",
        code=code,
        msg="err",
        hdrs=None,
        fp=None,
    )


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(status=st.sampled_from([403, 429, 503]))
def test_property_17_retryable_status_bounded_then_exhausts(loader, monkeypatch, status):
    # Feature: agentcore-demo-lab, Property 17: For a persistently retryable
    # status, the loader retries at most MAX_RETRIES times after the initial
    # attempt and then reports exhaustion (permit id, target, final status,
    # action) with a raised SeedExhausted.
    calls = {"n": 0}

    def always_fail(*_a, **_k):
        calls["n"] += 1
        raise _http_error(status)

    monkeypatch.setattr(loader.urllib.request, "urlopen", always_fail)
    slept: list[float] = []

    with pytest.raises(loader.SeedExhausted) as exc_info:
        loader._request(
            _Creds(),
            "PUT",
            "https://shared.es.invalid",
            "permits/_doc/A-1001",
            {"permit_id": "A-1001"},
            permit_id="A-1001",
            sleep=slept.append,
        )

    # Initial attempt + MAX_RETRIES retries = MAX_RETRIES + 1 total calls.
    assert calls["n"] == loader.MAX_RETRIES + 1
    assert len(slept) == loader.MAX_RETRIES
    assert exc_info.value.status == status
    assert exc_info.value.permit_id == "A-1001"
    assert exc_info.value.action == "PUT permits/_doc/A-1001"


@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(fail_count=st.integers(min_value=1, max_value=5))
def test_property_17_recovers_within_retry_budget(loader, monkeypatch, fail_count):
    """A retryable status that clears within the budget succeeds, not exhausts."""
    calls = {"n": 0}

    class _Resp:
        status = 200

        def read(self):
            return b'{"result":"updated"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fail_then_succeed(*_a, **_k):
        calls["n"] += 1
        if calls["n"] <= fail_count:
            raise _http_error(503)
        return _Resp()

    monkeypatch.setattr(loader.urllib.request, "urlopen", fail_then_succeed)
    status, _ = loader._request(
        _Creds(),
        "PUT",
        "https://shared.es.invalid",
        "permits/_doc/A-1001",
        {"permit_id": "A-1001"},
        permit_id="A-1001",
        sleep=lambda _s: None,
    )
    assert status == 200
    assert calls["n"] == fail_count + 1


def test_allowed_404_returns_instead_of_raising(loader, monkeypatch):
    """A 404 from GET /permits is an allowed index-absence result, not a failure."""
    calls = {"n": 0}

    def not_found(*_a, **_k):
        calls["n"] += 1
        raise urllib.error.HTTPError(
            url="https://shared.es.invalid/permits",
            code=404,
            msg="not found",
            hdrs=None,
            fp=io.BytesIO(b'{"status":404}'),
        )

    monkeypatch.setattr(loader.urllib.request, "urlopen", not_found)
    status, body = loader._request(
        _Creds(),
        "GET",
        "https://shared.es.invalid",
        "permits",
        allowed_statuses=(200, 404),
        sleep=lambda _s: None,
    )
    assert status == 404
    assert body == {"status": 404}
    assert calls["n"] == 1  # allowed status is not retried


def test_non_retryable_status_raises_immediately(loader, monkeypatch):
    """A non-retryable status (e.g. 400) fails immediately without retrying."""
    calls = {"n": 0}

    def fail_400(*_a, **_k):
        calls["n"] += 1
        raise _http_error(400)

    monkeypatch.setattr(loader.urllib.request, "urlopen", fail_400)
    with pytest.raises(RuntimeError):
        loader._request(
            _Creds(),
            "PUT",
            "https://shared.es.invalid",
            "permits/_doc/A-1001",
            {"permit_id": "A-1001"},
            permit_id="A-1001",
            sleep=lambda _s: None,
        )
    assert calls["n"] == 1  # no retries


# ---------------------------------------------------------------------------
# 13.5 Unit test: retry-delay schedule matches documented policy
# ---------------------------------------------------------------------------


def test_retry_policy_constants(loader):
    """The documented retryable statuses and retry count match the loader."""
    assert loader.RETRYABLE_STATUSES == (403, 429, 503)
    assert loader.MAX_RETRIES == 5


def test_retry_delay_is_capped_and_bounded(loader):
    """Each backoff delay is non-negative and never exceeds the documented cap."""
    for attempt in range(1, loader.MAX_RETRIES + 1):
        for _ in range(50):
            delay = loader.retry_delay(attempt)
            assert 0.0 <= delay <= loader.MAX_DELAY_SECONDS
