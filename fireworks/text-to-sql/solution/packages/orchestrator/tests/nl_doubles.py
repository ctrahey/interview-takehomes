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
from t2s_nl.intents import PLAN_SCHEMA_NAME


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
        reasoning_effort: str | None = None,
    ) -> InferenceResponse:
        self.calls.append(Call(list(messages), schema_name))
        if schema_name == PLAN_SCHEMA_NAME:
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
        reasoning_effort: str | None = None,
    ) -> InferenceResponse:
        self.calls.append(Call(list(messages), schema_name))
        # reasoning_effort MUST be forwarded: it is part of the fixture key, so
        # a wrapper that accepts it and drops it turns every replay into a
        # FixtureNotFound that looks like a prompt regression.
        return self.inner.complete(
            messages,
            response_schema=response_schema,
            schema_name=schema_name,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        )


def directive_payload(
    intent: str,
    *,
    inspect_target: str = "unspecified",
    table: str | None = None,
    text: str | None = None,
    row_count: int | None = None,
    model_ref: str | None = None,
    referent: str = "none",
    rationale: str = "test",
) -> dict[str, Any]:
    """One directive, wire-shaped (D15)."""
    return {
        "intent": intent,
        "parameters": {
            "inspect_target": inspect_target,
            "table": table,
            "model_ref": model_ref,
            "row_count": row_count,
            "seed": None,
            "text": text,
        },
        "referent": referent,
        "rationale": rationale,
    }


def plan_payload(
    *directives: dict[str, Any],
    confidence: str = "high",
    clarifying_question: str | None = None,
) -> dict[str, Any]:
    """A whole router response: an ordered list of directives (D15)."""
    return {
        "directives": list(directives),
        "confidence": confidence,
        "clarifying_question": clarifying_question,
    }


def router_payload(
    intent: str,
    *,
    inspect_target: str = "unspecified",
    table: str | None = None,
    text: str | None = None,
    row_count: int | None = None,
    model_ref: str | None = None,
    referent: str = "none",
    rationale: str = "test",
    clarifying_question: str | None = None,
) -> dict[str, Any]:
    """A one-directive plan -- the common case, kept terse for the many tests
    that care about a single intent and nothing about plans."""
    return plan_payload(
        directive_payload(
            intent,
            inspect_target=inspect_target,
            table=table,
            text=text,
            row_count=row_count,
            # Forwarded, deliberately and explicitly: a wrapper that accepts an
            # argument and quietly drops it is the bug that orphaned every
            # fixture it touched, twice in this project.
            model_ref=model_ref,
            referent=referent,
            rationale=rationale,
        ),
        clarifying_question=clarifying_question,
    )


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
        reasoning_effort: str | None = None,
    ) -> InferenceResponse:
        raise AssertionError(
            "an inference call was made on a path that must be fully deterministic"
        )
