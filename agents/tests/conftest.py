"""Shared pytest fixtures for the agents test project.

These fixtures mock every AWS and runtime boundary the property and unit tests
touch, so tests never make a live AWS call. Fixtures are deliberately generic:
they model the *shape* of each boundary (a signing key, an HTTP response, a
repository, a callback sink, a clock, a guardrail response) rather than binding
to implementation modules that do not exist yet.

Boundaries covered here:
  - JWT signing keys / key retrieval           -> ``jwt_signing_keys``, ``jwt_key_retriever``
  - OpenSearch HTTP responses                  -> ``opensearch_http``
  - DynamoDB repositories                      -> ``dynamodb_repository``, ``dynamodb_repositories``
  - Step Functions task-token callbacks        -> ``step_functions_callbacks``
  - Controllable clock / sleep (no real waits) -> ``fake_clock``
  - Bedrock ``ApplyGuardrail`` responses       -> ``apply_guardrail``
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Callable

import pytest


# ---------------------------------------------------------------------------
# JWT signing keys / key retrieval
# ---------------------------------------------------------------------------


@dataclass
class SigningKey:
    """A generated RSA key pair plus the metadata a JWT verifier needs.

    ``kid`` identifies the key in a JWKS-style lookup. ``private_pem`` signs
    tokens in a test; ``public_pem`` (and the ``jwk`` mapping) stand in for what
    a key-retrieval boundary would hand back to a verifier.
    """

    kid: str
    private_pem: bytes
    public_pem: bytes
    algorithm: str = "RS256"

    @property
    def jwk(self) -> dict[str, str]:
        return {"kid": self.kid, "alg": self.algorithm, "use": "sig", "kty": "RSA"}


@pytest.fixture
def jwt_signing_keys() -> list[SigningKey]:
    """Two deterministic RSA key pairs for signing and verifying test JWTs.

    Two keys let a test model key rotation and ``kid`` mismatch without any
    network call to a JWKS endpoint.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    keys: list[SigningKey] = []
    for kid in ("test-key-1", "test-key-2"):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_pem = private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        keys.append(SigningKey(kid=kid, private_pem=private_pem, public_pem=public_pem))
    return keys


@dataclass
class KeyRetriever:
    """Mocked key-retrieval boundary (stands in for a JWKS/OIDC lookup).

    ``get(kid)`` returns the matching key or ``None``. ``calls`` records every
    requested ``kid`` so a test can assert no unexpected network-style lookups
    happened. ``fail_with`` forces the retriever to raise, modelling an
    unreachable key endpoint.
    """

    keys: dict[str, SigningKey]
    calls: list[str] = field(default_factory=list)
    fail_with: Exception | None = None

    def get(self, kid: str) -> SigningKey | None:
        self.calls.append(kid)
        if self.fail_with is not None:
            raise self.fail_with
        return self.keys.get(kid)


@pytest.fixture
def jwt_key_retriever(jwt_signing_keys: list[SigningKey]) -> KeyRetriever:
    """A key retriever backed by the generated signing keys, no network access."""
    return KeyRetriever(keys={key.kid: key for key in jwt_signing_keys})


# ---------------------------------------------------------------------------
# OpenSearch HTTP responses
# ---------------------------------------------------------------------------


@dataclass
class FakeHTTPResponse:
    """Minimal HTTP response stand-in for an OpenSearch data-plane call."""

    status_code: int
    json_body: Any = None
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return self.json_body


@dataclass
class FakeOpenSearchHTTP:
    """Scripted OpenSearch HTTP boundary.

    Register responses per (method, path) with ``queue_response`` or fall back to
    ``default_response``. Every request is captured in ``requests`` for later
    assertions. No socket is ever opened.
    """

    default_response: FakeHTTPResponse | None = None
    _scripted: dict[tuple[str, str], list[FakeHTTPResponse]] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)

    def queue_response(self, method: str, path: str, response: FakeHTTPResponse) -> None:
        self._scripted.setdefault((method.upper(), path), []).append(response)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> FakeHTTPResponse:
        self.requests.append(
            {"method": method.upper(), "path": path, "body": body, "headers": headers or {}}
        )
        queue = self._scripted.get((method.upper(), path))
        if queue:
            return queue.pop(0)
        if self.default_response is not None:
            return self.default_response
        raise AssertionError(f"No scripted OpenSearch response for {method.upper()} {path}")


@pytest.fixture
def opensearch_http() -> FakeOpenSearchHTTP:
    """A scripted OpenSearch HTTP client that records requests and replays responses."""
    return FakeOpenSearchHTTP(
        default_response=FakeHTTPResponse(
            status_code=200, json_body={"hits": {"total": {"value": 0}, "hits": []}}
        )
    )


# ---------------------------------------------------------------------------
# DynamoDB repositories
# ---------------------------------------------------------------------------


@dataclass
class FakeDynamoDBRepository:
    """In-memory stand-in for a DynamoDB table repository.

    Models the append-only and conditional-write semantics the design relies on:
      - ``put`` supports an optional ``condition`` callable that rejects the write
        (raising ``ConditionalCheckFailed``) when it returns ``False``.
      - ``update``/``delete`` raise ``AppendOnlyViolation`` when
        ``append_only=True``, so an audit-table repository can prove immutability.
      - ``query`` returns items whose attributes match every supplied predicate.
    """

    name: str = "table"
    key_attr: str = "id"
    append_only: bool = False
    items: dict[Any, dict[str, Any]] = field(default_factory=dict)

    class ConditionalCheckFailed(Exception):
        pass

    class AppendOnlyViolation(Exception):
        pass

    def put(
        self,
        item: dict[str, Any],
        *,
        condition: Callable[[dict[str, Any] | None], bool] | None = None,
    ) -> dict[str, Any]:
        key = item[self.key_attr]
        existing = self.items.get(key)
        if self.append_only and existing is not None:
            raise self.AppendOnlyViolation(
                f"{self.name} is append-only; {self.key_attr}={key!r} already exists"
            )
        if condition is not None and not condition(existing):
            raise self.ConditionalCheckFailed(
                f"condition failed for {self.key_attr}={key!r} on {self.name}"
            )
        self.items[key] = dict(item)
        return self.items[key]

    def get(self, key: Any) -> dict[str, Any] | None:
        item = self.items.get(key)
        return dict(item) if item is not None else None

    def update(self, key: Any, changes: dict[str, Any]) -> dict[str, Any]:
        if self.append_only:
            raise self.AppendOnlyViolation(f"{self.name} does not allow UpdateItem")
        if key not in self.items:
            raise KeyError(key)
        self.items[key].update(changes)
        return dict(self.items[key])

    def delete(self, key: Any) -> None:
        if self.append_only:
            raise self.AppendOnlyViolation(f"{self.name} does not allow DeleteItem")
        self.items.pop(key, None)

    def query(self, **predicates: Any) -> list[dict[str, Any]]:
        return [
            dict(item)
            for item in self.items.values()
            if all(item.get(attr) == value for attr, value in predicates.items())
        ]


@pytest.fixture
def dynamodb_repository() -> FakeDynamoDBRepository:
    """A single generic in-memory DynamoDB repository."""
    return FakeDynamoDBRepository()


@pytest.fixture
def dynamodb_repositories() -> dict[str, FakeDynamoDBRepository]:
    """The three tables the design distinguishes: staging, records, audit.

    ``audit`` is append-only so a test can assert immutability. ``staging`` keys
    on ``approval_id`` and ``records`` on ``record_id`` to match the data model.
    """
    return {
        "staging": FakeDynamoDBRepository(name="staging", key_attr="approval_id"),
        "records": FakeDynamoDBRepository(name="records", key_attr="record_id"),
        "audit": FakeDynamoDBRepository(
            name="audit", key_attr="event_time_ns", append_only=True
        ),
    }


# ---------------------------------------------------------------------------
# Step Functions task-token callbacks
# ---------------------------------------------------------------------------


@dataclass
class FakeStepFunctionsCallbacks:
    """Records ``SendTaskSuccess``/``SendTaskFailure``/``SendTaskHeartbeat`` calls.

    A test asserts that a terminal proposal never resumes a token twice, and that
    a token is only ever resumed through this boundary. ``resumed_tokens`` gives a
    quick membership check.
    """

    successes: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    heartbeats: list[str] = field(default_factory=list)

    def send_task_success(self, task_token: str, output: Any = None) -> None:
        self.successes.append({"task_token": task_token, "output": output})

    def send_task_failure(
        self, task_token: str, error: str | None = None, cause: str | None = None
    ) -> None:
        self.failures.append({"task_token": task_token, "error": error, "cause": cause})

    def send_task_heartbeat(self, task_token: str) -> None:
        self.heartbeats.append(task_token)

    @property
    def resumed_tokens(self) -> set[str]:
        return {c["task_token"] for c in self.successes} | {
            c["task_token"] for c in self.failures
        }


@pytest.fixture
def step_functions_callbacks() -> FakeStepFunctionsCallbacks:
    """A Step Functions callback sink that records instead of calling AWS."""
    return FakeStepFunctionsCallbacks()


# ---------------------------------------------------------------------------
# Controllable clock / sleep (no real waits)
# ---------------------------------------------------------------------------


@dataclass
class FakeClock:
    """A frozen, manually advanced clock with a no-op sleep.

    ``now()`` returns the current epoch seconds; ``sleep(seconds)`` advances the
    clock and records the requested duration in ``slept`` without ever blocking.
    Tests assert on ``slept`` to verify a backoff schedule while running instantly.
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


# ---------------------------------------------------------------------------
# Bedrock ApplyGuardrail responses
# ---------------------------------------------------------------------------


@dataclass
class FakeApplyGuardrail:
    """Scripted Bedrock ``ApplyGuardrail`` boundary.

    Responses are queued and returned in order; ``calls`` captures each request
    (source, content) for assertions. The helper builders return responses in the
    shape ``ApplyGuardrail`` produces: an ``action`` (``NONE``/``GUARDRAIL_INTERVENED``),
    optional masked ``outputs`` text, and a contextual grounding score that a test
    can set to a number or ``None`` to exercise the absent-score path.
    """

    _queue: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def queue(self, response: dict[str, Any]) -> None:
        self._queue.append(response)

    def apply_guardrail(
        self, *, source: str, content: Any, **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append({"source": source, "content": content, **kwargs})
        if self._queue:
            return self._queue.pop(0)
        return self.allow_response(content)

    @staticmethod
    def allow_response(text: Any = None) -> dict[str, Any]:
        return {
            "action": "NONE",
            "outputs": [{"text": text}] if text is not None else [],
            "assessments": [],
            "groundingScore": None,
        }

    @staticmethod
    def masked_response(masked_text: str) -> dict[str, Any]:
        return {
            "action": "GUARDRAIL_INTERVENED",
            "outputs": [{"text": masked_text}],
            "assessments": [{"sensitiveInformationPolicy": {"piiEntities": ["MASKED"]}}],
            "groundingScore": None,
        }

    @staticmethod
    def blocked_response() -> dict[str, Any]:
        return {
            "action": "GUARDRAIL_INTERVENED",
            "outputs": [],
            "assessments": [{"topicPolicy": {"topics": ["prompt-attack"]}}],
            "groundingScore": None,
        }

    @staticmethod
    def grounding_response(score: float | None) -> dict[str, Any]:
        return {
            "action": "NONE",
            "outputs": [],
            "assessments": [
                {"contextualGroundingPolicy": {"filters": [{"score": score}]}}
            ],
            "groundingScore": score,
        }


@pytest.fixture
def apply_guardrail() -> FakeApplyGuardrail:
    """A scripted ``ApplyGuardrail`` client with allow/mask/block/grounding helpers."""
    return FakeApplyGuardrail()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def interaction_id_factory() -> Callable[[], str]:
    """Deterministic Interaction_ID generator for linking trace and audit records."""
    counter = itertools.count(1)

    def _factory() -> str:
        return f"interaction-{next(counter):04d}"

    return _factory
