"""Test doubles for layer 3. No network, no fixtures required.

Named ``nl_doubles`` rather than ``doubles`` on purpose: ``t2s_core/tests``
already ships a ``doubles`` module, and with pytest's importlib mode two
same-named top-level test helpers in one run resolve to whichever was imported
first. The prefix is ugly and it is also the fix.

``ScriptedClient`` answers by looking at which JSON schema it was handed, so one
object can serve the router, the query path, the schema path and the sample-data
path in a single conversation without the test caring about call order.

``TripwireClient`` wraps any client and records every message that reached it.
It is the instrument for the assertion this whole package exists to support: a
state answer must be derivable with the model having been told nothing about the
state, and having said nothing about it either.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from t2s_core.models import Usage
from t2s_core.ports import InferenceClient, InferenceResponse, Message
from t2s_nl.data import DATA_SCHEMA_NAME
from t2s_nl.intents import ROUTER_SCHEMA_NAME


@dataclass
class Call:
    messages: list[Message]
    schema_name: str

    @property
    def text(self) -> str:
        return "\n".join(m.content for m in self.messages)


@dataclass
class ScriptedClient:
    """Returns canned JSON, chosen by the response schema it was asked for."""

    router: list[dict[str, Any]] = field(default_factory=list)
    envelope: list[dict[str, Any]] = field(default_factory=list)
    data: list[dict[str, Any]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    model_name: str = "scripted/test-model"

    @property
    def model(self) -> str:
        return self.model_name

    def complete(
        self,
        messages: Sequence[Message],
        *,
        response_schema: Mapping[str, Any],
        schema_name: str = "response",
        max_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> InferenceResponse:
        self.calls.append(Call(list(messages), schema_name))
        if schema_name == ROUTER_SCHEMA_NAME:
            payload = self._next(self.router, "router")
        elif schema_name == DATA_SCHEMA_NAME:
            payload = self._next(self.data, "sample data")
        else:
            payload = self._next(self.envelope, "envelope")
        return InferenceResponse(
            content=json.dumps(payload),
            model=self.model_name,
            finish_reason="stop",
            usage=Usage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
            latency_ms=1,
        )

    @staticmethod
    def _next(queue: list[dict[str, Any]], label: str) -> dict[str, Any]:
        if not queue:
            raise AssertionError(f"ScriptedClient ran out of {label} responses")
        return queue.pop(0) if len(queue) > 1 else queue[0]


@dataclass
class TripwireClient:
    """Wraps a client and remembers everything that crossed the boundary."""

    inner: InferenceClient
    calls: list[Call] = field(default_factory=list)

    @property
    def model(self) -> str:
        return self.inner.model

    @property
    def sent_text(self) -> str:
        return "\n".join(c.text for c in self.calls)

    def complete(
        self,
        messages: Sequence[Message],
        *,
        response_schema: Mapping[str, Any],
        schema_name: str = "response",
        max_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> InferenceResponse:
        self.calls.append(Call(list(messages), schema_name))
        return self.inner.complete(
            messages,
            response_schema=response_schema,
            schema_name=schema_name,
            max_tokens=max_tokens,
            temperature=temperature,
        )


def router_payload(
    intent: str,
    *,
    inspect_target: str = "unspecified",
    table: str | None = None,
    text: str | None = None,
    row_count: int | None = None,
    rationale: str = "test",
    clarifying_question: str | None = None,
) -> dict[str, Any]:
    return {
        "intent": intent,
        "confidence": "high",
        "parameters": {
            "inspect_target": inspect_target,
            "table": table,
            "model_ref": None,
            "row_count": row_count,
            "seed": None,
            "text": text,
        },
        "clarifying_question": clarifying_question,
        "rationale": rationale,
    }


def envelope_payload(
    response_class: str,
    *,
    query: str | None = None,
    prose: str = "ok",
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "response_class": response_class,
        "query": query,
        "prose": prose,
        "error": error,
    }


@dataclass
class ExplodingClient:
    """Raises on any contact. Used to prove a code path needs no inference."""

    model_name: str = "exploding/none"

    @property
    def model(self) -> str:
        return self.model_name

    def complete(
        self,
        messages: Sequence[Message],
        *,
        response_schema: Mapping[str, Any],
        schema_name: str = "response",
        max_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> InferenceResponse:
        raise AssertionError(
            "an inference call was made on a path that must be fully deterministic"
        )
