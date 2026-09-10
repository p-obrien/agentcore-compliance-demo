"""Shared pytest fixtures for the seed loader test project.

The seed loader has two mockable boundaries: the OpenSearch managed-domain HTTP
endpoint (index create, mapping check, upsert, refresh) and time/sleep (its
bounded exponential backoff with jitter). Both are mocked here so tests run
instantly and never touch AWS.

Boundaries covered:
  - OpenSearch HTTP responses (with scripted retry sequences) -> ``opensearch_http``
  - Controllable clock / sleep (no real waits)                -> ``fake_clock``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# OpenSearch HTTP responses
# ---------------------------------------------------------------------------


@dataclass
class FakeHTTPResponse:
    """Minimal HTTP response stand-in for an OpenSearch managed-domain call."""

    status_code: int
    json_body: Any = None
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return self.json_body


@dataclass
class FakeOpenSearchHTTP:
    """Scripted OpenSearch HTTP boundary with retry-sequence support.

    ``queue_response`` appends one response for a (method, path) pair; queue
    several to model a 503-then-200 retry sequence. ``queue_status`` is a
    shorthand that queues a bare status code. Requests are captured in
    ``requests`` (including a per-key ``attempt`` count) so a test can assert how
    many retries the loader issued. When a queue is exhausted the
    ``default_response`` is returned, or an ``AssertionError`` is raised if none
    was configured. No socket is ever opened.
    """

    default_response: FakeHTTPResponse | None = None
    _scripted: dict[tuple[str, str], list[FakeHTTPResponse]] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)
    _attempts: dict[tuple[str, str], int] = field(default_factory=dict)

    def queue_response(self, method: str, path: str, response: FakeHTTPResponse) -> None:
        self._scripted.setdefault((method.upper(), path), []).append(response)

    def queue_status(self, method: str, path: str, status_code: int, json_body: Any = None) -> None:
        self.queue_response(method, path, FakeHTTPResponse(status_code=status_code, json_body=json_body))

    def queue_sequence(self, method: str, path: str, status_codes: list[int]) -> None:
        for status_code in status_codes:
            self.queue_status(method, path, status_code)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> FakeHTTPResponse:
        key = (method.upper(), path)
        self._attempts[key] = self._attempts.get(key, 0) + 1
        self.requests.append(
            {
                "method": key[0],
                "path": path,
                "body": body,
                "headers": headers or {},
                "attempt": self._attempts[key],
            }
        )
        queue = self._scripted.get(key)
        if queue:
            return queue.pop(0)
        if self.default_response is not None:
            return self.default_response
        raise AssertionError(f"No scripted OpenSearch response for {key[0]} {path}")

    def attempts(self, method: str, path: str) -> int:
        return self._attempts.get((method.upper(), path), 0)


@pytest.fixture
def opensearch_http() -> FakeOpenSearchHTTP:
    """A scripted OpenSearch HTTP client that records requests and replays responses."""
    return FakeOpenSearchHTTP(
        default_response=FakeHTTPResponse(status_code=200, json_body={"result": "updated"})
    )


# ---------------------------------------------------------------------------
# Controllable clock / sleep (no real waits)
# ---------------------------------------------------------------------------


@dataclass
class FakeClock:
    """A frozen, manually advanced clock with a no-op sleep.

    ``sleep(seconds)`` records the requested backoff delay in ``slept`` and
    advances the clock without blocking, so a bounded-retry test can assert the
    delay schedule while running instantly.
    """

    _now: float = 1_700_000_000.0
    slept: list[float] = field(default_factory=list)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self._now += seconds


@pytest.fixture
def fake_clock() -> FakeClock:
    """A controllable clock whose ``sleep`` never blocks the test run."""
    return FakeClock()
