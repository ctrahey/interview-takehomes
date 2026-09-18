"""Request, envelope and result models.

The response envelope has exactly one shape for all three outcomes (design §3).

Finding #2 from the live probe drives the most important decision in this file:
the wire JSON schema *cannot* express the cross-field invariants -- a field that
is ``required`` but nullable is satisfied by ``null``, and the model did exactly
that (``response_class: "error"`` with ``error: null``). So the invariants live
here, in Pydantic, and a violation is a **repairable outcome**, routed through the
same loop as a SQL binder error.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "Attempt",
    "Dialect",
    "ErrorDetail",
    "FailureKind",
    "ModelEnvelope",
    "QueryRequest",
    "QueryResult",
    "ResponseClass",
    "ResultMetadata",
    "SchemaRequest",
    "SchemaResult",
    "Usage",
    "Verdict",
    "VerdictKind",
]

Dialect = Literal["sqlite", "postgres", "mysql"]
ResponseClass = Literal["valid", "clarification_needed", "error"]

#: Why a candidate was rejected. These four are exactly the repairable classes;
#: the repair loop feeds every one of them back to the model the same way.
FailureKind = Literal["invalid_json", "envelope_invariant", "safety_gate", "binder"]

VerdictKind = Literal["ok", "safety_gate", "binder"]

EXECUTABLE_DIALECTS: frozenset[str] = frozenset({"sqlite"})


class ErrorDetail(BaseModel):
    """Machine-readable error, carried inside the envelope (not an exception)."""

    model_config = ConfigDict(extra="ignore")

    code: str = Field(default="unspecified")
    message: str
    details: str | None = None


class Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


class Verdict(BaseModel):
    """A validator's ruling on one candidate statement."""

    ok: bool
    kind: VerdictKind = "ok"
    error: str | None = None
    #: Free-form, validator-specific notes (e.g. rows sampled by the dry run).
    detail: dict[str, str | int | bool] = Field(default_factory=dict)

    @classmethod
    def passed(cls, **detail: str | int | bool) -> Verdict:
        return cls(ok=True, kind="ok", detail=dict(detail))

    @classmethod
    def failed(cls, kind: VerdictKind, error: str, **detail: str | int | bool) -> Verdict:
        return cls(ok=False, kind=kind, error=error, detail=dict(detail))


class Attempt(BaseModel):
    """One pass through generate → validate. Every pass is recorded, including the
    successful one, so the eval harness and the CLI can show the repair working."""

    index: int
    response_class: ResponseClass | None = None
    candidate_sql: str | None = None
    ok: bool
    failure_kind: FailureKind | None = None
    failure_message: str | None = None
    latency_ms: int = 0
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str | None = None


class ResultMetadata(BaseModel):
    model: str
    dialect: Dialect
    attempts: list[Attempt] = Field(default_factory=list)
    latency_ms: int = 0
    usage: Usage = Field(default_factory=Usage)
    validator: str = "none"
    repairs_used: int = 0
    prompt_versions: dict[str, str] = Field(default_factory=dict)


class ModelEnvelope(BaseModel):
    """The model's raw answer, after the cross-field invariants are enforced.

    Constructing this is the enforcement point. ``generate`` catches the resulting
    ``ValidationError`` and converts it into a repair turn.
    """

    model_config = ConfigDict(extra="ignore")

    response_class: ResponseClass
    query: str | None = None
    prose: str = ""
    error: ErrorDetail | None = None

    @model_validator(mode="after")
    def _enforce_invariants(self) -> ModelEnvelope:
        rc = self.response_class
        query = (self.query or "").strip()
        prose = (self.prose or "").strip()

        if rc == "valid":
            if not query:
                raise ValueError(
                    'response_class "valid" requires a non-empty "query"; it was null or empty.'
                )
            if self.error is not None:
                raise ValueError(
                    'response_class "valid" requires "error" to be null, but an error was given.'
                )
        elif rc == "clarification_needed":
            if query:
                raise ValueError(
                    'response_class "clarification_needed" requires "query" to be null, '
                    "but a query was given."
                )
            if not prose:
                raise ValueError(
                    'response_class "clarification_needed" requires "prose" to contain the '
                    "clarifying question to ask the user; it was empty."
                )
        else:  # "error"
            if self.error is None or not self.error.message.strip():
                raise ValueError(
                    'response_class "error" requires a non-null "error" object with a '
                    'non-empty "message"; it was null.'
                )
            if query:
                raise ValueError(
                    'response_class "error" requires "query" to be null, but a query was given.'
                )
        return self


class _Envelope(BaseModel):
    """Shared shape of the two public results."""

    response_class: ResponseClass
    query: str | None = None
    prose: str = ""
    error: ErrorDetail | None = None
    metadata: ResultMetadata


class QueryResult(_Envelope):
    """Result of :func:`t2s_core.generate_query`. ``query`` holds one SELECT."""


class SchemaResult(_Envelope):
    """Result of :func:`t2s_core.generate_schema`. ``query`` holds the DDL script."""


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    schema_ddl: str = Field(min_length=1)
    dialect: Dialect = "sqlite"
    session_summary: str | None = None
    max_repair_attempts: Annotated[int, Field(ge=0, le=5)] = 2


class SchemaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    dialect: Dialect = "sqlite"
    session_summary: str | None = None
    max_repair_attempts: Annotated[int, Field(ge=0, le=5)] = 2
