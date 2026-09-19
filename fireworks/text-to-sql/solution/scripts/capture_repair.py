#!/usr/bin/env python
"""Capture a real self-correction into ``docs/evidence/self-correction.md``.

Nothing here is staged. The first attempt is whatever the model actually
returned; if the run produces no rejection, the script says so and writes
nothing rather than manufacturing one. A fabricated repair demo would be the
single most dishonest thing this repository could contain, because the repair
loop is precisely the feature it is meant to evidence.

    FIREWORKS_API_KEY=... uv run python scripts/capture_repair.py

Model choice is deliberate and disclosed in the artifact. See the README: on a
frontier code model the loop almost never fires, because the schema is in the
prompt and the model can simply read the column names. The loop earns its place
on cheaper models, which is also the argument for having it.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import SecretStr

from t2s_core import FireworksClient, FireworksConfig, QueryRequest, generate_query

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "evidence"

#: Cheap models fail in ways frontier models do not; that is the point.
MODEL = os.environ.get("T2S_REPAIR_MODEL", "accounts/fireworks/models/muse-glimmer-30b")

QUESTION = "Which customers placed orders in every month of 2024?"

#: Fewer than this means the model never got rejected, so there is nothing to show.
MIN_ATTEMPTS_FOR_A_REPAIR = 2


def _key() -> str:
    env = os.environ.get("FIREWORKS_API_KEY")
    if env:
        return env.strip()
    path = Path.home() / ".fireworks-key"
    if not path.exists():
        sys.exit("No API key: set FIREWORKS_API_KEY or create ~/.fireworks-key")
    return path.read_text(encoding="utf-8").strip()


def main() -> int:
    ddl = (REPO / "evals" / "corpus" / "schemas" / "retail" / "ddl.sql").read_text(encoding="utf-8")
    client = FireworksClient(
        FireworksConfig(api_key=SecretStr(_key()), model=MODEL, max_tokens=3000)
    )
    result = generate_query(
        QueryRequest(question=QUESTION, schema_ddl=ddl, dialect="sqlite"), client=client
    )
    attempts = result.metadata.attempts

    if len(attempts) < MIN_ATTEMPTS_FOR_A_REPAIR:
        print(
            f"No rejection occurred on {MODEL} for this question "
            f"({len(attempts)} attempt, class={result.response_class}).\n"
            "Nothing written -- this script will not invent a first attempt."
        )
        return 1

    lines = [
        "# Self-correction, captured live",
        "",
        f"Captured {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} against `{MODEL}`.",
        "Regenerate with `uv run python scripts/capture_repair.py`.",
        "",
        "**The model is chosen deliberately and it is not our primary one.** On "
        "`kimi-k2p7-code` this loop almost never fires: the schema is in the prompt, so the "
        "model can read the column names, and six models were tried before one produced a "
        "rejection worth showing. That is itself the finding -- the loop is insurance whose "
        "value scales inversely with model strength, and it is what makes a cheap model "
        "usable at all.",
        "",
        f"Question asked: _{QUESTION}_",
        "",
    ]

    for attempt in attempts:
        lines.append(f"## Attempt {attempt.index + 1} — {'accepted' if attempt.ok else 'rejected'}")
        lines.append("")
        if attempt.candidate_sql:
            lines.append(f"```sql\n{attempt.candidate_sql.strip()}\n```")
        else:
            lines.append("_No SQL in this response._")
        lines.append("")
        if not attempt.ok:
            lines.append(f"Rejected by the validator as `{attempt.failure_kind}`:")
            lines.append("")
            lines.append(f"> {attempt.failure_message}")
            lines.append("")
            source = (
                "SQLite's own words, verbatim"
                if attempt.failure_kind == "binder"
                else "our validator's words, not a paraphrase of them"
            )
            lines.append(f"That exact text is fed back to the model as the next turn -- {source}.")
            lines.append("")

    sqls = [a.candidate_sql.strip() for a in attempts if a.candidate_sql]
    if len(sqls) > 1 and len(set(sqls)) == 1:
        lines.append("## Note: the SQL did not change")
        lines.append("")
        lines.append(
            "Both attempts carry identical SQL. The rejection here was an "
            "**envelope** failure -- the model omitted a required field of the response "
            "contract -- not a bad query. This is the failure mode cheap models actually "
            "exhibit, and it is exactly why the repair loop funnels envelope violations, "
            "safety-gate rejections and binder errors through one path: from the caller's "
            "side they are all 'the model produced something we can prove is wrong'."
        )
        lines.append("")
        lines.append(
            "The binder branch -- where the *SQL itself* is rejected by SQLite and rewritten "
            "-- is covered by unit tests using a seeded first attempt that is explicitly "
            "flagged synthetic in the fixture data. It is not reproduced here live because "
            "no model tried would produce a bind-failing query on demand; see the README."
        )
        lines.append("")
    lines.append("## Outcome")
    lines.append("")
    lines.append(
        f"`{result.response_class}` after {len(attempts)} attempts, "
        f"validated by `{result.metadata.validator}`."
    )
    lines.append("")
    lines.append(
        "Every attempt is retained in `metadata.attempts`, which is what lets the CLI and "
        "the chat render the trail instead of silently presenting the final answer as if it "
        "had been the first."
    )
    lines.append("")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "self-correction.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT / 'self-correction.md'} ({len(attempts)} attempts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
