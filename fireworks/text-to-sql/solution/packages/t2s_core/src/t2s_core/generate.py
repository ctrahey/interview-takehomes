"""The public surface (design §3) and the repair loop behind it.

The loop is one code path with four ways in, and that unification is the design
decision worth reading:

    generate → parse → check invariants → safety gate → bind-check
                 |            |               |            |
            invalid_json  envelope_      safety_gate    binder
                          invariant

All four are ``RepairableFailure``. A broken cross-field invariant (finding #2)
is *not* a client error and *not* an exception -- it is exactly as repairable as
``no such column: custmer_id``, and it is re-prompted through the same template.
That is why the model returning ``response_class: "error"`` with ``error: null``
costs one extra turn instead of an outage.

Every attempt -- including the successful one -- is recorded in
``metadata.attempts`` with its candidate SQL and the verdict that rejected it.
The eval harness reports on it and the CLI prints it, so it must not collapse.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import ValidationError
from sqlglot import parse as sqlglot_parse
from sqlglot.errors import ParseError

from t2s_core.config import DEFAULT_MAX_TOKENS, DEFAULT_SCHEMA_MAX_TOKENS
from t2s_core.errors import RepairableFailure
from t2s_core.models import (
    Attempt,
    Dialect,
    ErrorDetail,
    FailureKind,
    ModelEnvelope,
    QueryRequest,
    QueryResult,
    ResponseClass,
    ResultMetadata,
    SchemaRequest,
    SchemaResult,
    Usage,
    Verdict,
)
from t2s_core.ports import InferenceClient, Message, QueryValidator, SchemaValidator
from t2s_core.prompts import REGISTRY
from t2s_core.validation import default_validator
from t2s_core.validation.safety import sqlglot_dialect
from t2s_core.wire import ENVELOPE_SCHEMA, SCHEMA_NAME

__all__ = ["generate_query", "generate_schema"]


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------
def generate_query(
    req: QueryRequest,
    *,
    client: InferenceClient,
    validator: QueryValidator | None = None,
) -> QueryResult:
    """Natural-language question + DDL → one read-only SELECT, validated.

    ``validator`` defaults to :class:`~t2s_core.validation.EphemeralSqliteValidator`
    for SQLite and :class:`~t2s_core.validation.SqlglotValidator` otherwise. Pass
    :class:`~t2s_core.validation.NoOpValidator` to disable the repair loop.
    """
    checker = validator if validator is not None else default_validator(req.dialect)
    preflight = _preflight_ddl(req.schema_ddl, req.dialect)
    if preflight is not None:
        return QueryResult(
            response_class="error",
            prose=preflight.message,
            error=preflight,
            metadata=ResultMetadata(
                model=client.model, dialect=req.dialect, validator=_name(checker)
            ),
        )

    messages = [
        Message(
            "system",
            REGISTRY.render("query.system", dialect=req.dialect, schema_ddl=req.schema_ddl),
        ),
        Message(
            "user",
            REGISTRY.render(
                "query.user",
                session_context=_session_context(req.session_summary),
                question=req.question,
            ),
        ),
    ]
    outcome = _run_loop(
        messages,
        client=client,
        dialect=req.dialect,
        max_repair_attempts=req.max_repair_attempts,
        max_tokens=DEFAULT_MAX_TOKENS,
        artifact="SQL query",
        validate=lambda sql: checker.check(sql, req.schema_ddl, req.dialect),
        validator_name=_name(checker),
        prompt_names=("query.system", "query.user", "common.repair"),
    )
    return QueryResult(
        response_class=outcome.response_class,
        query=outcome.query,
        prose=outcome.prose,
        error=outcome.error,
        metadata=outcome.metadata,
    )


def generate_schema(
    req: SchemaRequest,
    *,
    client: InferenceClient,
    validator: SchemaValidator | None = None,
) -> SchemaResult:
    """Natural-language domain description → DDL for ``req.dialect``.

    ``validator`` is a :class:`~t2s_core.ports.SchemaValidator` rather than a
    ``QueryValidator``: the query gate forbids DDL outright, so the schema path
    needs its own mirror gate (CREATE-only, no DML/DROP/ATTACH/PRAGMA). All three
    shipped validators implement both protocols, so one object serves both entry
    points.
    """
    checker = validator if validator is not None else default_validator(req.dialect)
    messages = [
        Message("system", REGISTRY.render("schema.system", dialect=req.dialect)),
        Message(
            "user",
            REGISTRY.render(
                "schema.user",
                session_context=_session_context(req.session_summary),
                description=req.description,
            ),
        ),
    ]
    outcome = _run_loop(
        messages,
        client=client,
        dialect=req.dialect,
        max_repair_attempts=req.max_repair_attempts,
        max_tokens=DEFAULT_SCHEMA_MAX_TOKENS,
        artifact="DDL script",
        validate=lambda ddl: checker.check_ddl(ddl, req.dialect),
        validator_name=_name(checker),
        prompt_names=("schema.system", "schema.user", "common.repair"),
    )
    return SchemaResult(
        response_class=outcome.response_class,
        query=outcome.query,
        prose=outcome.prose,
        error=outcome.error,
        metadata=outcome.metadata,
    )


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _Outcome:
    envelope: ModelEnvelope | None
    failure: RepairableFailure | None


@dataclass(slots=True)
class _LoopResult:
    """The envelope fields, before they are stamped into a QueryResult or a
    SchemaResult. Both results have identical shapes; only their docstrings and
    the meaning of ``query`` differ."""

    response_class: ResponseClass
    query: str | None
    prose: str
    error: ErrorDetail | None
    metadata: ResultMetadata


def _run_loop(
    messages: Sequence[Message],
    *,
    client: InferenceClient,
    dialect: Dialect,
    max_repair_attempts: int,
    max_tokens: int,
    artifact: str,
    validate: Callable[[str], Verdict],
    validator_name: str,
    prompt_names: tuple[str, ...],
) -> _LoopResult:
    conversation = list(messages)
    attempts: list[Attempt] = []
    total_usage = Usage()
    started = time.perf_counter()
    failure: RepairableFailure | None = None

    for index in range(max_repair_attempts + 1):
        response = client.complete(
            conversation,
            response_schema=ENVELOPE_SCHEMA,
            schema_name=SCHEMA_NAME,
            max_tokens=max_tokens,
            temperature=0.0,
        )
        total_usage = total_usage + response.usage
        outcome = _evaluate(response.content, validate)
        failure = outcome.failure
        envelope = outcome.envelope

        attempts.append(
            Attempt(
                index=index,
                response_class=envelope.response_class if envelope else None,
                candidate_sql=(envelope.query if envelope else None)
                or (failure.candidate_sql if failure else None),
                ok=failure is None,
                failure_kind=_kind(failure),
                failure_message=failure.message if failure else None,
                latency_ms=response.latency_ms,
                usage=response.usage,
                finish_reason=response.finish_reason,
            )
        )

        if failure is None and envelope is not None:
            return _LoopResult(
                response_class=envelope.response_class,
                query=envelope.query,
                prose=envelope.prose,
                error=envelope.error,
                metadata=_metadata(
                    client,
                    dialect,
                    attempts=attempts,
                    started=started,
                    usage=total_usage,
                    validator_name=validator_name,
                    prompt_names=prompt_names,
                ),
            )

        if index < max_repair_attempts:
            conversation.append(Message("assistant", response.content))
            conversation.append(
                Message(
                    "user",
                    REGISTRY.render(
                        "common.repair",
                        artifact=artifact,
                        candidate=(failure.candidate_sql if failure else None)
                        or "(the previous response contained no SQL)",
                        failure_kind=_kind(failure) or "unknown",
                        failure_message=failure.message if failure else "unknown",
                    ),
                )
            )

    # Budget exhausted. The envelope still has one shape -- this is an "error"
    # outcome carrying the full attempt trace, not an exception.
    detail = failure.message if failure else "unknown"
    kind = _kind(failure) or "unknown"
    return _LoopResult(
        response_class="error",
        query=None,
        prose=(
            f"I could not produce a working {artifact.lower()} after "
            f"{len(attempts)} attempt(s); the last problem was: {detail}"
        ),
        error=ErrorDetail(
            code=f"repair_exhausted.{kind}",
            message=f"No candidate passed validation within {max_repair_attempts} repair attempts.",
            details=detail,
        ),
        metadata=_metadata(
            client,
            dialect,
            attempts=attempts,
            started=started,
            usage=total_usage,
            validator_name=validator_name,
            prompt_names=prompt_names,
        ),
    )


def _evaluate(content: str, validate: Callable[[str], Verdict]) -> _Outcome:
    """Parse → invariants → validate. Every failure here is repairable."""
    try:
        envelope = ModelEnvelope.model_validate_json(content)
    except ValidationError as exc:
        kind: FailureKind = (
            "invalid_json"
            if any(e["type"].startswith("json_") for e in exc.errors())
            else "envelope_invariant"
        )
        return _Outcome(
            envelope=None,
            failure=RepairableFailure(
                kind, _format_validation_error(exc), candidate_sql=_peek_query(content)
            ),
        )

    if envelope.response_class != "valid" or envelope.query is None:
        return _Outcome(envelope=envelope, failure=None)

    verdict = validate(envelope.query)
    if verdict.ok:
        return _Outcome(envelope=envelope, failure=None)
    return _Outcome(
        envelope=envelope,
        failure=RepairableFailure(
            "safety_gate" if verdict.kind == "safety_gate" else "binder",
            verdict.error or "rejected by the validator",
            candidate_sql=envelope.query,
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _preflight_ddl(schema_ddl: str, dialect: Dialect) -> ErrorDetail | None:
    """D9: supplied DDL is attacker-controlled text. It is parsed before it is
    ever interpolated into a prompt, and a parse failure costs zero model calls.
    The API layer maps ``invalid_schema_ddl`` to a 422."""
    try:
        statements = [s for s in sqlglot_parse(schema_ddl, dialect=sqlglot_dialect(dialect)) if s]
    except ParseError as exc:
        return ErrorDetail(
            code="invalid_schema_ddl",
            message="The supplied schema DDL could not be parsed, so no query was generated.",
            details=str(exc)[:500],
        )
    if not statements:
        return ErrorDetail(
            code="invalid_schema_ddl",
            message="The supplied schema DDL contains no statements.",
            details=None,
        )
    return None


def _session_context(summary: str | None) -> str:
    if not summary or not summary.strip():
        return ""
    return REGISTRY.render("common.session_context", session_summary=summary.strip())


def _name(checker: object) -> str:
    name = getattr(checker, "name", None)
    return str(name) if isinstance(name, str) else type(checker).__name__


def _kind(failure: RepairableFailure | None) -> FailureKind | None:
    if failure is None:
        return None
    kind: FailureKind = failure.kind  # type: ignore[assignment]
    return kind


def _peek_query(content: str) -> str | None:
    """Best-effort: pull ``query`` out of a response whose *invariants* failed."""
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(parsed, dict):
        value = parsed.get("query")
        return value if isinstance(value, str) and value.strip() else None
    return None


def _format_validation_error(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(piece) for piece in error["loc"]) or "response"
        message = error["msg"].removeprefix("Value error, ")
        parts.append(f"{location}: {message}")
    return " ".join(parts) or str(exc)


def _metadata(
    client: InferenceClient,
    dialect: Dialect,
    *,
    attempts: list[Attempt],
    started: float,
    usage: Usage,
    validator_name: str,
    prompt_names: tuple[str, ...],
) -> ResultMetadata:
    return ResultMetadata(
        model=client.model,
        dialect=dialect,
        attempts=attempts,
        latency_ms=int((time.perf_counter() - started) * 1000),
        usage=usage,
        validator=validator_name,
        repairs_used=max(len(attempts) - 1, 0),
        prompt_versions=REGISTRY.pinned_versions(*prompt_names),
    )
