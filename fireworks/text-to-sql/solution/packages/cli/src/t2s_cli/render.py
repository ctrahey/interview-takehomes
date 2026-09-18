"""Human-readable rendering of t2s_core envelopes (design §6).

This module is the reason the CLI is the live-demo surface: the repair loop is
the system's most interesting behaviour and it is invisible unless it is
rendered. ``render_envelope`` walks ``metadata.attempts`` and shows, for every
attempt, the candidate SQL, whether it was accepted or rejected, and -- when
rejected -- the exact validator/engine error that rejected it. Nothing here
decides pass/fail; it only displays what ``t2s_core`` already decided.

Every function returns plain strings (with ANSI codes already embedded via
``click.style`` when colour is requested); callers are responsible for writing
them out through ``click.echo(..., color=use_color)`` so that ``--no-color``
and non-TTY output degrade the same way everywhere.
"""

from __future__ import annotations

import click
import sqlglot

from t2s_core.models import Attempt, QueryResult, ResponseClass, SchemaResult

__all__ = ["pretty_sql", "render_attempt", "render_result_lines"]

_SQLGLOT_DIALECT = {"sqlite": "sqlite", "postgres": "postgres", "mysql": "mysql"}

_STATUS_COLOR = {"valid": "green", "clarification_needed": "yellow", "error": "red"}
_STATUS_LABEL = {
    "valid": "VALID",
    "clarification_needed": "CLARIFICATION NEEDED",
    "error": "ERROR",
}


def pretty_sql(sql: str | None, dialect: str) -> str:
    """Best-effort pretty-print via sqlglot. Falls back to the raw text for
    anything that does not parse -- e.g. a candidate peeked out of a response
    whose JSON envelope itself failed to validate."""
    if sql is None or not sql.strip():
        return ""
    target = _SQLGLOT_DIALECT.get(dialect, dialect)
    try:
        statements = sqlglot.transpile(sql, read=target, write=target, pretty=True)
        rendered = "\n\n".join(statements).strip()
        return rendered or sql.strip()
    except Exception:  # noqa: BLE001 -- rendering must never crash the CLI
        return sql.strip()


def _indent(text: str, prefix: str = "      ") -> str:
    return "\n".join(prefix + line if line else line for line in text.splitlines())


def render_attempt(
    attempt: Attempt, *, total: int, dialect: str, use_color: bool, code_label: str = "SQL"
) -> list[str]:
    """One attempt in the repair trail: candidate, verdict, and -- when
    rejected -- the engine/validator error that rejected it."""
    lines: list[str] = []
    ordinal = attempt.index + 1
    if attempt.ok:
        badge = click.style("ACCEPTED", fg="green", bold=True) if use_color else "ACCEPTED"
    else:
        badge = click.style("REJECTED", fg="red", bold=True) if use_color else "REJECTED"
    kind_note = f" ({attempt.failure_kind})" if attempt.failure_kind else ""
    header = f"Attempt {ordinal}/{total} — {badge}{kind_note}"
    lines.append(click.style(header, bold=True) if use_color else header)

    if attempt.candidate_sql:
        lines.append(f"    {code_label}:")
        lines.append(_indent(pretty_sql(attempt.candidate_sql, dialect)))
    elif attempt.response_class and attempt.response_class != "valid":
        note = f"    (no {code_label} — model returned {attempt.response_class!r})"
        lines.append(note)

    if not attempt.ok and attempt.failure_message:
        error_line = f"    engine error: {attempt.failure_message}"
        lines.append(click.style(error_line, fg="red") if use_color else error_line)

    return lines


def render_result_lines(
    result: QueryResult | SchemaResult,
    *,
    dialect: str,
    use_color: bool,
    code_label: str = "SQL",
) -> list[str]:
    """The full human-readable rendering: the repair trail, then the final
    outcome. ``result`` is a ``QueryResult`` or ``SchemaResult`` -- both share
    the envelope shape (design §3)."""
    response_class: ResponseClass = result.response_class
    metadata = result.metadata
    attempts = metadata.attempts
    lines: list[str] = []

    if attempts:
        lines.append(click.style("Repair trail:", bold=True) if use_color else "Repair trail:")
        for attempt in attempts:
            lines.extend(
                render_attempt(
                    attempt,
                    total=len(attempts),
                    dialect=dialect,
                    use_color=use_color,
                    code_label=code_label,
                )
            )
            lines.append("")

    color = _STATUS_COLOR.get(response_class, "white")
    label = _STATUS_LABEL.get(response_class, response_class.upper())
    heading = f"Result: {label}"
    lines.append(click.style(heading, fg=color, bold=True) if use_color else heading)

    if response_class == "valid":
        lines.append(_indent(pretty_sql(result.query, dialect), prefix="  "))
        if result.prose:
            lines.append(f"  — {result.prose}")
    elif response_class == "clarification_needed":
        lines.append(f"  {result.prose}")
    else:  # error
        if result.prose:
            lines.append(f"  {result.prose}")
        if result.error is not None:
            lines.append(f"  code: {result.error.code}")
            if result.error.details:
                lines.append(f"  details: {result.error.details}")

    summary = (
        f"  model={metadata.model} dialect={metadata.dialect} validator={metadata.validator} "
        f"repairs_used={metadata.repairs_used} latency_ms={metadata.latency_ms} "
        f"tokens={metadata.usage.total_tokens}"
    )
    lines.append(click.style(summary, dim=True) if use_color else summary)

    return lines
