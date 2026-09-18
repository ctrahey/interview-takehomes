"""Test doubles, importable by the test modules (see conftest.py for the path hook)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from t2s_core.models import Usage
from t2s_core.ports import InferenceResponse, Message

RETAIL_DDL = """
CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    city        TEXT
);
CREATE TABLE orders (
    order_id    INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    total_cents INTEGER NOT NULL,
    placed_at   TEXT NOT NULL
);
"""


def envelope(
    response_class: str = "valid",
    query: str | None = None,
    prose: str = "Explains the query.",
    error: dict[str, Any] | None = None,
) -> str:
    """Serialize a model answer exactly as the wire schema would deliver it."""
    return json.dumps(
        {"response_class": response_class, "query": query, "prose": prose, "error": error}
    )


class ScriptedClient:
    """An InferenceClient that returns canned completion bodies in order.

    Records the messages it was given, which is how the repair-loop tests assert
    that the failing SQL and the engine error were actually re-prompted.
    """

    def __init__(self, contents: Sequence[str], *, model: str = "scripted/model") -> None:
        self._contents = list(contents)
        self._model = model
        self.calls: list[list[Message]] = []
        self.max_tokens_seen: list[int | None] = []

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
        self.calls.append(list(messages))
        self.max_tokens_seen.append(max_tokens)
        if not self._contents:
            raise AssertionError("ScriptedClient ran out of scripted responses")
        content = self._contents.pop(0)
        return InferenceResponse(
            content=content,
            model=self._model,
            finish_reason="stop",
            usage=Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            latency_ms=7,
        )
