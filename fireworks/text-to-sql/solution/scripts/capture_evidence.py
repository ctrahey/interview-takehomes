#!/usr/bin/env python
"""Capture the end-to-end flow as a committed artifact.

The claim this repo makes is not a benchmark score; it is that a generated
query is *checked against a real database built from your own schema* before
you ever see it, and adapted when the engine rejects it. A claim like that is
worth exactly as much as the transcript proving it, so this script produces one
and writes it to ``docs/evidence/``.

    FIREWORKS_API_KEY=... uv run python scripts/capture_evidence.py

Everything here is live. Nothing is staged, and no first attempt is
hand-authored -- see ``capture_repair.py`` for why that distinction matters.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import SecretStr

from t2s_core import (
    FireworksClient,
    FireworksConfig,
    QueryRequest,
    SchemaRequest,
    default_validator,
    generate_query,
    generate_schema,
)
from t2s_core.ports import QueryValidator

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "evidence"

DOMAIN = (
    "a climbing gym: members on membership plans, routes with difficulty grades, "
    "and check-ins recording when a member visited"
)
QUESTION = "Which membership plan has the most check-ins? Show the plan name and the count."


def _key() -> str:
    env = os.environ.get("FIREWORKS_API_KEY")
    if env:
        return env.strip()
    path = Path.home() / ".fireworks-key"
    if not path.exists():
        sys.exit("No API key: set FIREWORKS_API_KEY or create ~/.fireworks-key")
    return path.read_text(encoding="utf-8").strip()


def _emit_header(say: Callable[..., None], model: str) -> None:
    say("# End-to-end evidence")
    say()
    say(f"Captured {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} against `{model}`.")
    say("Regenerate with `uv run python scripts/capture_evidence.py`.")
    say()
    say("Every step below is a live call. Timings are wall-clock.")
    say()


def _emit_real_database(say: Callable[..., None], ddl: str) -> list[str]:
    """Build the schema for real, so the next section is checking something that exists."""
    say("## 2. Stand it up as a real database")
    say()
    conn = sqlite3.connect(":memory:")
    conn.executescript(ddl)
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    say(f"Created {len(tables)} tables: {', '.join(f'`{t}`' for t in tables)}.")
    say()
    say(
        "This database has **no rows in it**, and that is the point of the next step: "
        "the query is checked against it anyway."
    )
    say()
    return tables


def _emit_validator_probes(
    say: Callable[..., None],
    validator: QueryValidator,
    ddl: str,
    tables: list[str],
) -> None:
    """Tier 1, demonstrated against the empty database we just built."""
    probes = [
        (
            "a column that does not exist",
            f"SELECT {tables[0]}.definitely_not_a_column FROM {tables[0]}",
        ),
        ("a table that does not exist", "SELECT * FROM members_typo"),
        ("a real query", f"SELECT COUNT(*) FROM {tables[0]}"),
    ]
    say("| probe | verdict | engine said |")
    say("| --- | --- | --- |")
    for label, sql in probes:
        verdict = validator.check(sql, ddl, "sqlite")
        engine = (verdict.error or "-").replace("|", "\\|")
        say(f"| {label} | {'accepted' if verdict.ok else '**rejected**'} | `{engine[:60]}` |")


def main() -> int:
    model = os.environ.get("T2S_MODEL", "accounts/fireworks/models/kimi-k2p7-code")
    client = FireworksClient(
        FireworksConfig(api_key=SecretStr(_key()), model=model, max_tokens=4000)
    )
    lines: list[str] = []

    def say(text: str = "") -> None:
        lines.append(text)
        print(text, flush=True)

    _emit_header(say, model)

    # 1 -- describe a domain, get DDL
    say("## 1. Describe a domain in English, get DDL")
    say()
    say(f"> {DOMAIN}")
    say()
    started = time.perf_counter()
    schema = generate_schema(SchemaRequest(description=DOMAIN, dialect="sqlite"), client=client)
    ddl_ms = int((time.perf_counter() - started) * 1000)
    ddl = schema.query
    if schema.response_class != "valid" or not ddl:
        say(f"FAILED: {schema.response_class} -- {schema.prose}")
        return 1
    say(f"```sql\n{ddl.strip()}\n```")
    say(f"_{ddl_ms} ms._")
    say()

    # 2 -- stand the schema up as a real database
    tables = _emit_real_database(say, ddl)

    # 3 -- the validator, against the empty database
    say("## 3. Every query is bind-checked against that database, empty or not")
    say()
    _emit_validator_probes(say, default_validator("sqlite"), ddl, tables)
    say()
    say(
        "No sample data was required for any of that. This tier is always on, for every "
        "generated query, and it is what makes a syntactically-plausible-but-wrong query "
        "impossible to hand back."
    )
    say()

    # 4 -- a real question
    say("## 4. Ask a question in English")
    say()
    say(f"> {QUESTION}")
    say()
    started = time.perf_counter()
    result = generate_query(
        QueryRequest(question=QUESTION, schema_ddl=ddl, dialect="sqlite"), client=client
    )
    q_ms = int((time.perf_counter() - started) * 1000)
    if result.response_class != "valid" or not result.query:
        say(f"Model answered `{result.response_class}`: {result.prose}")
        return 1
    say(f"```sql\n{result.query.strip()}\n```")
    say(
        f"_{q_ms} ms, {len(result.metadata.attempts)} attempt(s), "
        f"validated by `{result.metadata.validator}`._"
    )
    say()
    say(f"{result.prose}")
    say()

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "end-to-end.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT / 'end-to-end.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
