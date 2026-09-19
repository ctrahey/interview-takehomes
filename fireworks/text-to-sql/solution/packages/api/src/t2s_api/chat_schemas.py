"""Wire models for the conversational surface (layer 3 over HTTP).

Everything a browser needs to render one utterance is here, and it is all
typed: the plan the router produced (D15), the turn each directive produced,
and the append-only activity rows each one appended (D14). There is no bare
``dict`` in the interesting payloads -- a generated client gets
``turn.sql``, ``turn.table.rows`` and ``turn.attempts[i].failure_message``,
not ``Any``. The one deliberately open field is ``ActivityOut.detail``, which
D14 defines as free-form JSON per activity kind.

Three shapes carry the whole contract and are worth naming:

* :class:`DirectiveResult` pairs **one directive with the turn it produced**,
  so a two-directive utterance is two results in order rather than one
  flattened answer.
* :class:`ChatResponse.status` distinguishes ``completed`` from ``halted``
  from ``refused``. A halt is *not* an error: the completed prefix is in
  ``results`` and the directives that never ran are in ``not_run``, because a
  client has to be able to render "this much happened, then it stopped, and
  here is why".
* ``TurnOut.kind`` carries ``clarification_needed`` / ``error`` on a **200**,
  exactly as layer 2 does with ``response_class`` (design §5). Only transport
  and validation failures are HTTP errors, and those are problem+json.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from t2s_core.models import Attempt
from t2s_nl.activity import ActivityRecord
from t2s_nl.intents import Confidence, Directive, Intent
from t2s_nl.turns import TurnKind

__all__ = [
    "ActivityOut",
    "ActivityPage",
    "ChatRequest",
    "ChatResponse",
    "ChatStatus",
    "DataTableOut",
    "DirectiveResult",
    "PlanOut",
    "TurnOut",
]

#: How one chat call ended.
#:
#: ``completed`` -- every directive in the plan ran.
#: ``halted``    -- a directive did not answer, so the rest were not run (D15).
#: ``refused``   -- the plan was over the cap and *nothing* ran (D15).
ChatStatus = Literal["completed", "halted", "refused"]


class ChatRequest(BaseModel):
    """One thing said to the system, in natural language."""

    model_config = ConfigDict(extra="forbid")

    utterance: str = Field(
        min_length=1,
        max_length=4000,
        description=(
            "What the user said. May contain several directives ('load sample data and "
            "then show me a query for unpaid balances'); the router decides how many, and "
            "each one is executed and gated separately."
        ),
    )


class DataTableOut(BaseModel):
    """Tabular output. Always from a deterministic read, never from a model."""

    model_config = ConfigDict(from_attributes=True)

    columns: list[str]
    rows: list[list[str]] = Field(description="Row values, already stringified for display.")
    caption: str | None = None
    truncated: bool = False
    total_rows: int | None = Field(
        default=None, description="Rows before truncation, when the reader knows it."
    )
    row_count: int = Field(description="len(rows) -- what is actually in this payload.")


class TurnOut(BaseModel):
    """What one directive produced.

    ``kind`` mirrors ``t2s_core``'s response envelope on purpose:
    ``clarification_needed`` and ``error`` are ordinary conversational turns
    carried on a 200, not HTTP failures.
    """

    model_config = ConfigDict(from_attributes=True)

    kind: TurnKind
    intent: Intent
    text: str = Field(description="The prose half of the answer, or the question/error text.")
    sql: str | None = None
    ddl: str | None = None
    table: DataTableOut | None = None
    attempts: list[Attempt] = Field(
        default_factory=list,
        description="Every pass through generate → validate, including the successful one (D4).",
    )
    notes: list[str] = Field(default_factory=list)
    deterministic_answer: bool = Field(
        default=False,
        description=(
            "True when every fact in this turn came from foundation or a sample database "
            "rather than from a model."
        ),
    )
    plan_position: int = Field(default=1, description="1-based position within the plan.")
    plan_length: int = 1


class ActivityOut(BaseModel):
    """One append-only activity row (D14). Never updated, never deleted."""

    model_config = ConfigDict(from_attributes=True)

    seq: int = Field(description="Per-session, monotonic. The pagination and SSE event id.")
    session_id: uuid.UUID
    kind: str = Field(description="e.g. 'router.classify', 'query.generate', 'query.execute'.")
    phase: Literal["begin", "end"]
    status: str = Field(description="'running' on a begin row; 'ok' | 'error' | 'refused' on end.")
    at: datetime
    duration_ms: int | None = Field(default=None, description="Set on `end` rows only.")
    summary: str | None = None
    detail: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Free-form JSON whose shape is defined per kind by D14 -- e.g. "
            "{'sql': ..., 'question': ...} on query.generate. Deliberately open."
        ),
    )
    model: str | None = Field(default=None, description="Set only where inference was involved.")
    tokens: int | None = None
    request_id: str | None = Field(
        default=None, description="The provider's opaque request handle. Never a credential."
    )

    @classmethod
    def from_record(cls, record: ActivityRecord) -> ActivityOut:
        """From the detached view listeners are handed (`t2s_nl.activity`).

        The live stream and the paginated read must produce byte-identical
        payloads for the same row -- a client that reconnects and re-reads a
        row it already had must not see it change -- so both go through a
        model built here rather than each formatting its own dict.
        """
        return cls(
            seq=record.seq,
            session_id=record.session_id,
            kind=record.kind,
            phase="begin" if record.phase == "begin" else "end",
            status=record.status,
            at=record.at,
            duration_ms=record.duration_ms,
            summary=record.summary,
            detail=dict(record.detail) if record.detail else None,
            model=record.model,
            tokens=record.tokens,
            request_id=record.request_id,
        )


class PlanOut(BaseModel):
    """The router's ruling on one utterance: an ordered list of directives (D15)."""

    directives: list[Directive]
    confidence: Confidence
    clarifying_question: str | None = None
    refusal: str | None = Field(
        default=None,
        description=(
            "Set when the plan was over the cap. Refused whole -- no directive ran, and "
            "`results` holds a single error turn carrying this text."
        ),
    )
    activity_seqs: list[int] = Field(
        default_factory=list,
        description="The `router.classify` activity rows this classification appended.",
    )


class DirectiveResult(BaseModel):
    """One directive and the turn it produced, with the activities it appended."""

    position: int = Field(description="1-based position in the plan.")
    directive: Directive | None = Field(
        default=None,
        description="Null only for the synthetic turn that reports a refused plan.",
    )
    turn: TurnOut
    activity_seqs: list[int] = Field(
        default_factory=list,
        description="`seq` of every activity row appended while this directive ran.",
    )


class ChatResponse(BaseModel):
    """The executed plan: what was asked, what ran, and why it stopped if it did."""

    session_id: uuid.UUID
    utterance: str
    status: ChatStatus
    plan: PlanOut
    results: list[DirectiveResult] = Field(
        description="One entry per directive that ran, in plan order."
    )
    not_run: list[Directive] = Field(
        default_factory=list,
        description="Directives the halt or the refusal prevented from running.",
    )
    stopped_reason: str | None = Field(
        default=None,
        description="Why execution stopped short. Null when status is 'completed'.",
    )
    activities: list[ActivityOut] = Field(
        default_factory=list,
        description=(
            "Every activity row appended while serving this request, in `seq` order -- the "
            "same rows the SSE stream delivered live."
        ),
    )


class ActivityPage(BaseModel):
    """A page of the append-only log. Read-only: there is no write endpoint."""

    session_id: uuid.UUID
    activities: list[ActivityOut]
    after_seq: int | None = Field(description="The cursor this page was read from (echoed).")
    limit: int
    next_after_seq: int | None = Field(
        default=None,
        description=(
            "Pass as `after_seq` to fetch the next page, or as the SSE stream's `after_seq` "
            "to switch to live updates with no gap. Null when the page is empty."
        ),
    )
    has_more: bool = Field(
        description="True when more rows already exist past this page (limit was reached)."
    )
