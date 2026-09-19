"""What one conversational turn returns.

A ``Turn`` is data, not text: prose plus an optional table, optional SQL, and
the repair-loop attempts. Rendering is ``t2s_nl.render``'s job and the chat
REPL's; a Slack bot or an HTTP endpoint would render the same ``Turn``
differently without the orchestrator changing.

``kind`` has exactly three values, mirroring ``t2s_core``'s response envelope on
purpose: ``clarification_needed`` and ``error`` are ordinary turns in a
conversation, not exceptions. The chat loop never sees a traceback for either.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from t2s_core.models import Attempt
from t2s_nl.intents import Intent

__all__ = ["DataTable", "Turn", "TurnKind"]

TurnKind = Literal["answer", "clarification_needed", "error"]


@dataclass(slots=True)
class DataTable:
    """Tabular output. ``rows`` are already stringified for display.

    Every ``DataTable`` in this package is built from a deterministic read --
    either ``foundation``'s metadata store or a gated query against a sample
    database. None is ever built from model output.
    """

    columns: list[str]
    rows: list[list[str]]
    caption: str | None = None
    truncated: bool = False
    total_rows: int | None = None

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass(slots=True)
class Turn:
    kind: TurnKind = "answer"
    intent: Intent = "unknown"
    text: str = ""
    table: DataTable | None = None
    sql: str | None = None
    ddl: str | None = None
    attempts: list[Attempt] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: True when the factual content of this turn came from foundation or a
    #: sample database rather than from an LLM. Asserted in the test suite.
    deterministic_answer: bool = False

    @classmethod
    def error(cls, text: str, *, intent: Intent = "unknown", **kwargs: Any) -> Turn:
        return cls(kind="error", intent=intent, text=text, **kwargs)

    @classmethod
    def ask(cls, question: str, *, intent: Intent = "unknown", **kwargs: Any) -> Turn:
        return cls(kind="clarification_needed", intent=intent, text=question, **kwargs)
