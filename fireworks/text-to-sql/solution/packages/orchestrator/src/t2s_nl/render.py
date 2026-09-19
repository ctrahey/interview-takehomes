"""Turning a ``Turn`` into lines of text.

Kept out of the chat loop so it can be tested without a terminal, and so a
different front end (Slack, HTTP) can reuse the parts it wants. Two things here
are requirements rather than decoration:

* **The repair loop is rendered.** When a candidate SQL is rejected by the
  binder or the safety gate and the model fixes it on the next turn, that is the
  most interesting thing the system does, and it is completely invisible unless
  someone prints it. :func:`repair_lines` prints the rejected SQL, the engine's
  own error text, and then the fix.
* **Tables say when they were capped.** A truncated result that looks complete
  is a lie told by a rendering bug.
"""

from __future__ import annotations

import os
import sys

import sqlglot

from t2s_core.models import Attempt
from t2s_nl.turns import DataTable, Turn

__all__ = ["COLORS", "colorize", "pretty_sql", "render_table", "render_turn", "repair_lines"]

MAX_CELL = 40
DEFAULT_MAX_ROWS = 25

COLORS = {
    "dim": "\033[2m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
    "reset": "\033[0m",
}


def _enabled() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def colorize(key: str, text: str) -> str:
    if not _enabled():
        return text
    return f"{COLORS[key]}{text}{COLORS['reset']}"


def pretty_sql(sql: str, dialect: str = "sqlite") -> str:
    try:
        return sqlglot.transpile(sql, read=dialect, pretty=True)[0]
    except Exception:  # noqa: BLE001 - formatting must never break a turn
        return sql.strip()


def render_table(table: DataTable, *, max_rows: int = DEFAULT_MAX_ROWS) -> list[str]:
    if not table.rows:
        # An empty result is a sentence ("no sample databases yet"), not a
        # header row over nothing -- column names with no data under them read
        # as a rendering bug.
        return [f"  {table.caption}"] if table.caption else []

    rows = table.rows[:max_rows]
    capped = len(table.rows) > max_rows
    headers = table.columns or [""] * (len(rows[0]) if rows else 0)
    widths = []
    for index, header in enumerate(headers):
        cells = [str(r[index]) if index < len(r) else "" for r in rows]
        widths.append(
            min(MAX_CELL, max(len(header), *(len(c) for c in cells)) if cells else len(header))
        )

    lines: list[str] = []
    show_header = any(h for h in headers)
    if show_header:
        lines.append(
            "  " + "  ".join(h[:w].ljust(w) for h, w in zip(headers, widths, strict=False))
        )
        lines.append("  " + "  ".join("-" * w for w in widths))
    for row in rows:
        lines.append(
            "  " + "  ".join(str(cell)[:w].ljust(w) for cell, w in zip(row, widths, strict=False))
        )
    footer = f"  {len(rows)} row(s)"
    if capped:
        footer += f" — showing {max_rows} of {len(table.rows)}"
    if table.truncated:
        footer += " — capped by the engine; there are more"
    if table.rows:
        lines.append(colorize("dim", footer))
    return lines


def repair_lines(attempts: list[Attempt], dialect: str = "sqlite") -> list[str]:
    """Show every rejected candidate and the error that rejected it.

    Silent when there was nothing to repair -- a one-attempt success prints
    nothing, so the noise appears exactly when it is interesting.
    """
    if len(attempts) <= 1:
        return []
    lines: list[str] = []
    for attempt in attempts:
        if attempt.ok:
            lines.append(colorize("green", f"  attempt {attempt.index}: accepted"))
            continue
        lines.append(
            colorize("yellow", f"  attempt {attempt.index}: rejected ({attempt.failure_kind})")
        )
        if attempt.candidate_sql:
            for line in pretty_sql(attempt.candidate_sql, dialect).splitlines():
                lines.append(colorize("dim", f"      {line}"))
        if attempt.failure_message:
            lines.append(colorize("red", f"      → {attempt.failure_message}"))
    return lines


def render_turn(turn: Turn, *, dialect: str = "sqlite", max_rows: int = DEFAULT_MAX_ROWS) -> str:
    lines: list[str] = []
    lines.extend(repair_lines(turn.attempts, dialect))

    if turn.kind == "clarification_needed":
        lines.append(colorize("yellow", f"  ? {turn.text}"))
    elif turn.kind == "error":
        lines.append(colorize("red", f"  ! {turn.text}"))
    elif turn.text:
        lines.append(f"  {turn.text}")

    if turn.ddl:
        lines.append(colorize("green", "  DDL"))
        lines.extend(f"    {line}" for line in turn.ddl.strip().splitlines())
    if turn.sql:
        lines.append(colorize("green", "  SQL"))
        lines.extend(f"    {line}" for line in pretty_sql(turn.sql, dialect).splitlines())
    if turn.table is not None:
        if turn.table.caption and turn.table.caption != turn.text:
            lines.append(colorize("dim", f"  {turn.table.caption}"))
        # An empty table renders AS its caption sentence, so when that sentence
        # is already the turn's text, rendering it would say the same thing
        # twice ("no sample databases yet" / "no sample databases yet").
        if turn.table.rows or turn.table.caption != turn.text:
            lines.extend(render_table(turn.table, max_rows=max_rows))
    for note in turn.notes:
        lines.append(colorize("dim", f"  · {note}"))
    return "\n".join(lines)
