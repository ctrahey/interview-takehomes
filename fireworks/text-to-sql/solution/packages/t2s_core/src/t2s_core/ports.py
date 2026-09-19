"""Ports (design §3). Everything t2s_core needs from the outside world is one of
these three Protocols, so the whole package runs offline and deterministically.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from t2s_core.models import Usage, Verdict

__all__ = [
    "InferenceClient",
    "InferenceResponse",
    "Message",
    "QueryValidator",
    "SchemaValidator",
]

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str

    def as_wire(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class InferenceResponse:
    """One completion. ``content`` is the raw JSON *text* -- parsing is the
    caller's job, because a parse failure is a repairable outcome, not a
    transport failure."""

    content: str
    model: str
    finish_reason: str
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    request_id: str | None = None


@runtime_checkable
class InferenceClient(Protocol):
    """Chat completion with an enforced JSON schema (D7)."""

    def complete(
        self,
        messages: Sequence[Message],
        *,
        response_schema: Mapping[str, Any],
        schema_name: str = "response",
        max_tokens: int | None = None,
        temperature: float = 0.0,
        reasoning_effort: str | None = None,
    ) -> InferenceResponse: ...

    @property
    def model(self) -> str: ...


@runtime_checkable
class QueryValidator(Protocol):
    """Rules on a candidate SELECT. ``schema_ddl`` is the DDL from the *request*."""

    def check(self, sql: str, schema_ddl: str, dialect: str) -> Verdict: ...

    @property
    def name(self) -> str: ...


@runtime_checkable
class SchemaValidator(Protocol):
    """Rules on a candidate DDL script produced by ``generate_schema``.

    Deliberately a separate port from :class:`QueryValidator`: the query gate
    *forbids* DDL, so the two cannot share one ``check``. All three shipped
    validators implement both protocols, so callers can pass one object to both
    entry points.
    """

    def check_ddl(self, ddl: str, dialect: str) -> Verdict: ...

    @property
    def name(self) -> str: ...
