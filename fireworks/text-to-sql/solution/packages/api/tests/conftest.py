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

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import StaticPool

from t2s_api.app import create_app
from t2s_core.fixtures import recorded_client
from t2s_core.models import Usage
from t2s_core.ports import InferenceClient, InferenceResponse, Message
from t2s_nl.data import DATA_SCHEMA_NAME
from t2s_nl.intents import PLAN_SCHEMA_NAME


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
        reasoning_effort: str | None = None,
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


# ---------------------------------------------------------------------------
# Layer 3 (the conversational endpoints) -- W15
# ---------------------------------------------------------------------------
# `packages/orchestrator/tests/nl_doubles.py` has equivalents of the helpers
# below. They are re-stated here rather than imported for the reason this
# file's docstring already gives: cross-package test-helper imports resolve by
# collection order under `--import-mode=importlib`, and a suite that passes or
# fails depending on which package pytest visited first is not a suite. The
# copy is ~60 lines and it keeps this package's tests self-contained.


class ScriptedChatClient:
    """An `InferenceClient` that answers by which response schema it was handed.

    One object therefore serves the router, the query/schema envelope path and
    the sample-data path within a single conversation, with no assumption about
    call order. The last queued payload for a channel repeats, so a test that
    cares about one call does not have to enumerate the rest.
    """

    def __init__(
        self,
        *,
        router: Sequence[Mapping[str, Any]] = (),
        envelope: Sequence[Mapping[str, Any]] = (),
        data: Sequence[Mapping[str, Any]] = (),
        model: str = "scripted/test-model",
    ) -> None:
        self.router = [dict(p) for p in router]
        self.envelope = [dict(p) for p in envelope]
        self.data = [dict(p) for p in data]
        self.schema_names: list[str] = []
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
        reasoning_effort: str | None = None,
    ) -> InferenceResponse:
        self.schema_names.append(schema_name)
        if schema_name == PLAN_SCHEMA_NAME:
            payload = self._next(self.router, "router")
        elif schema_name == DATA_SCHEMA_NAME:
            payload = self._next(self.data, "sample data")
        else:
            payload = self._next(self.envelope, "envelope")
        return InferenceResponse(
            content=json.dumps(payload),
            model=self._model,
            finish_reason="stop",
            usage=Usage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
            latency_ms=1,
        )

    @staticmethod
    def _next(queue: list[dict[str, Any]], label: str) -> dict[str, Any]:
        if not queue:
            raise AssertionError(f"ScriptedChatClient ran out of {label} responses")
        return queue.pop(0) if len(queue) > 1 else queue[0]


def directive_payload(
    intent: str,
    *,
    inspect_target: str = "unspecified",
    table: str | None = None,
    text: str | None = None,
    row_count: int | None = None,
    referent: str = "none",
    rationale: str = "test",
) -> dict[str, Any]:
    return {
        "intent": intent,
        "parameters": {
            "inspect_target": inspect_target,
            "table": table,
            "model_ref": None,
            "row_count": row_count,
            "seed": None,
            "text": text,
        },
        "referent": referent,
        "rationale": rationale,
    }


def plan_payload(
    *directives: Mapping[str, Any],
    confidence: str = "high",
    clarifying_question: str | None = None,
) -> dict[str, Any]:
    return {
        "directives": [dict(d) for d in directives],
        "confidence": confidence,
        "clarifying_question": clarifying_question,
    }


def envelope_payload(
    response_class: str,
    *,
    query: str | None = None,
    prose: str = "ok",
    error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "response_class": response_class,
        "query": query,
        "prose": prose,
        "error": dict(error) if error else None,
    }


@pytest.fixture
def chat_doubles() -> SimpleNamespace:
    """The scripted client and the wire-shaped payload builders, in one handle."""
    return SimpleNamespace(
        ScriptedChatClient=ScriptedChatClient,
        directive=directive_payload,
        plan=plan_payload,
        envelope=envelope_payload,
    )


@pytest.fixture
def sample_db_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sample databases land in a temp directory -- never the developer's ~/.t2s."""
    directory = tmp_path / "sample_dbs"
    directory.mkdir()
    monkeypatch.setenv("T2S_SAMPLE_DB_DIR", str(directory))
    return directory


@pytest.fixture
def make_app() -> Callable[[InferenceClient], FastAPI]:
    """The app itself (not a TestClient), for tests that need `app.state`."""

    def _make(inference_client: InferenceClient) -> FastAPI:
        return create_app(engine=offline_engine(), inference_client=inference_client)

    return _make


def new_session(client: TestClient) -> str:
    """Create a session over HTTP and return its id."""
    response = client.post("/sessions", json={})
    assert response.status_code == 201, response.text
    session_id: str = response.json()["id"]
    return session_id


@pytest.fixture
def make_session() -> Callable[[TestClient], str]:
    return new_session
