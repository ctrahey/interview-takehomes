"""Fixture replay and capture (D8) -- the reason the whole repo is testable offline.

The key derivation is the load-bearing part. A fixture key must be:

* **stable across runs** -- so no timestamps, no ``id()``, no PYTHONHASHSEED
  sensitivity (hence canonical JSON, not ``hash()``);
* **insensitive to dict ordering** -- ``sort_keys=True`` throughout, so a
  reordered JSON schema still hits the same fixture;
* **sensitive to everything that changes the answer** -- model, every message in
  order, the response schema, the token budget, and the temperature.

Capture is the inverse: :class:`RecordingClient` wraps a live client and writes
each exchange to ``<dir>/<key>.json``. The same key function is used for both
directions, so a captured fixture is replayable by construction.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from t2s_core.config import DEFAULT_MODEL
from t2s_core.errors import FixtureNotFound
from t2s_core.models import Usage
from t2s_core.ports import InferenceClient, InferenceResponse, Message

__all__ = ["RecordedClient", "RecordingClient", "request_key"]

logger = logging.getLogger("t2s_core.inference")

KEY_LENGTH = 32


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_key(
    *,
    model: str,
    messages: Sequence[Message],
    response_schema: Mapping[str, Any],
    schema_name: str,
    max_tokens: int | None,
    temperature: float,
    reasoning_effort: str | None = None,
) -> str:
    # Only mixed into the key when actually set, so fixtures captured before
    # this parameter existed keep their keys instead of all orphaning at once.
    extra = {"reasoning_effort": reasoning_effort} if reasoning_effort is not None else {}
    material = _canonical(
        {
            "model": model,
            "messages": [[m.role, m.content] for m in messages],
            "response_schema": response_schema,
            "schema_name": schema_name,
            "max_tokens": max_tokens,
            "temperature": temperature,
            **extra,
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:KEY_LENGTH]


class RecordedClient:
    """Replays captured fixtures. Never touches the network."""

    def __init__(self, fixture_dir: Path | str, *, model: str | None = None) -> None:
        self.fixture_dir = Path(fixture_dir)
        self._model = model
        self._cache: dict[str, dict[str, Any]] = {}

    @property
    def model(self) -> str:
        return self._model or DEFAULT_MODEL

    def keys(self) -> list[str]:
        return sorted(p.stem for p in self.fixture_dir.glob("*.json"))

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
        key = request_key(
            model=self.model,
            messages=messages,
            response_schema=response_schema,
            schema_name=schema_name,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
        )
        fixture = self._load(key)
        recorded = fixture["response"]
        return InferenceResponse(
            content=recorded["content"],
            model=recorded.get("model", self.model),
            finish_reason=recorded.get("finish_reason", "stop"),
            usage=Usage.model_validate(recorded.get("usage") or {}),
            latency_ms=int(recorded.get("latency_ms", 0)),
            request_id=recorded.get("request_id"),
        )

    def _load(self, key: str) -> dict[str, Any]:
        if key in self._cache:
            return self._cache[key]
        path = self.fixture_dir / f"{key}.json"
        if not path.exists():
            raise FixtureNotFound(key, str(self.fixture_dir))
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        self._cache[key] = data
        return data


class RecordingClient:
    """Wraps a live client and writes every exchange to a fixture file.

    Used by ``scripts/capture_fixtures.py``; never used in the test suite, which
    would make the suite need a network.
    """

    def __init__(
        self,
        inner: InferenceClient,
        fixture_dir: Path | str,
        *,
        overwrite: bool = False,
    ) -> None:
        self.inner = inner
        self.fixture_dir = Path(fixture_dir)
        self.fixture_dir.mkdir(parents=True, exist_ok=True)
        self.overwrite = overwrite
        self.written: list[str] = []

    @property
    def model(self) -> str:
        return self.inner.model

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
        response = self.inner.complete(
            messages,
            response_schema=response_schema,
            schema_name=schema_name,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
        )
        key = request_key(
            model=self.model,
            messages=messages,
            response_schema=response_schema,
            schema_name=schema_name,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
        )
        path = self.fixture_dir / f"{key}.json"
        if path.exists() and not self.overwrite:
            logger.info("fixture %s already exists; not overwriting", key)
            return response
        payload = {
            "key": key,
            "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "request": {
                "model": self.model,
                "messages": [m.as_wire() for m in messages],
                "schema_name": schema_name,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "response_schema_sha256": hashlib.sha256(
                    _canonical(response_schema).encode("utf-8")
                ).hexdigest(),
            },
            "response": {
                "content": response.content,
                "model": response.model,
                "finish_reason": response.finish_reason,
                "usage": response.usage.model_dump(),
                "latency_ms": response.latency_ms,
                "request_id": response.request_id,
            },
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.written.append(key)
        return response
