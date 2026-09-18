"""Test configuration for `t2s_api`'s offline behavioural suite (D8).

Every test builds its own app via `create_app`, wired to an offline
`InferenceClient` (never a live `FireworksClient`) and a fresh in-memory,
`StaticPool`-backed SQLAlchemy engine so tests don't share state or touch
disk. This is the "app must be constructible with a RecordedClient"
requirement (design §5) made concrete: DI wiring, not a monkeypatch.

Deliberately exposes everything through fixtures rather than plain top-level
`import` statements from test modules: `t2s_core/tests/doubles.py` already
claims the generic module name ``doubles`` elsewhere in this workspace, and
with ``--import-mode=importlib`` a bare ``import doubles`` from here could
silently resolve to the wrong one depending on collection order. Fixtures
sidestep that entirely.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import StaticPool

from t2s_api.app import create_app
from t2s_core.fixtures import recorded_client
from t2s_core.ports import InferenceClient, InferenceResponse, Message


def offline_engine() -> Engine:
    """A private, in-memory metadata store -- never the module-level `app`'s engine."""
    return create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )


class RaisingClient:
    """An `InferenceClient` that raises a fixed exception on every call.

    Exercises the API's mapping of t2s_core's transport failures
    (`TruncatedResponse`, `RateLimited`, `UpstreamError`, ...) to HTTP status
    codes without touching the network.
    """

    def __init__(self, exc: Exception, *, model: str = "raising/model") -> None:
        self._exc = exc
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    def complete(
        self,
        messages: Sequence[Message],
        *,
        response_schema: Mapping[str, Any],
        schema_name: str = "response",
        max_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> InferenceResponse:
        raise self._exc


@pytest.fixture
def offline_client() -> InferenceClient:
    """Replays the fixtures shipped with t2s_core (D8) -- real captured model output,
    zero network."""
    return recorded_client()


@pytest.fixture
def client(offline_client: InferenceClient) -> Iterator[TestClient]:
    app = create_app(engine=offline_engine(), inference_client=offline_client)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def make_client() -> Callable[[InferenceClient], TestClient]:
    """For tests that need a custom `InferenceClient` (e.g. one that raises)."""

    def _make(inference_client: InferenceClient) -> TestClient:
        app = create_app(engine=offline_engine(), inference_client=inference_client)
        return TestClient(app)

    return _make


@pytest.fixture
def raising_client() -> type[RaisingClient]:
    return RaisingClient
