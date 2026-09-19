"""Driving layer 3 (`t2s_nl.Orchestrator`) from an HTTP request.

Three jobs, and the reason each is here rather than in the orchestrator:

1. **Binding a conversation to *this* app's engine.** `t2s_nl.store.Store`
   opens its own engine from a URL because `t2s-chat` is a process that owns
   its database. The API already owns one (`app.state.session_factory`), and
   two engines onto one SQLite file is how you get a locked database. So
   :class:`ApiStore` is the same interface over an engine handed in from
   outside, pointed at an explicitly named session rather than "the default
   one". Subclassing rather than editing `Store` keeps the change on this
   side of the layer boundary.

2. **Running a plan and keeping the plan.** `Orchestrator.run()` is the real
   entry point (`handle()` returns only the last turn of a plan and would
   silently drop the first half of a compound utterance). We call
   :meth:`~t2s_nl.orchestrator.Orchestrator.plan` and
   :meth:`~t2s_nl.orchestrator.Orchestrator.execute_plan` separately --
   exactly what `run()` does internally -- because the HTTP response has to
   carry the directives themselves, not just their turns.

3. **Attributing activities to directives.** Everything is sequential inside
   one turn, so a listener that records rows as they are appended, with a
   boundary marked at each completed turn, partitions the log exactly. No
   correlation ids, no guessing: the `router.classify` rows land before the
   first boundary, and each directive's rows land between consecutive ones.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Engine

from foundation.repositories import SessionRepository
from t2s_api.chat_schemas import (
    ActivityOut,
    ChatResponse,
    ChatStatus,
    DataTableOut,
    DirectiveResult,
    PlanOut,
    TurnOut,
)
from t2s_nl.activity import ActivityRecord
from t2s_nl.intents import Plan
from t2s_nl.orchestrator import Orchestrator
from t2s_nl.store import Store
from t2s_nl.turns import Turn

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.orm import Session as OrmSession
    from sqlalchemy.orm import sessionmaker

    from t2s_core.ports import InferenceClient
    from t2s_nl.activity import ActivityListener

__all__ = ["ApiStore", "SessionNotFound", "run_chat"]


class SessionNotFound(LookupError):
    """The path's `session_id` names no session in this deployment's store."""


class ApiStore(Store):
    """A `Store` over an engine the app already owns, bound to a named session.

    Deliberately does **not** call `Store.__init__`: that constructor's job is
    to create an engine, create the schema, and resolve-or-create a default
    session, and all three are already someone else's job here. The attributes
    it sets are set below, and the inherited `scope()` is the whole of the
    behaviour layer 3 actually uses.
    """

    def __init__(
        self,
        engine: Engine,
        session_factory: sessionmaker[OrmSession],
        session_id: uuid.UUID,
    ) -> None:
        # Password-redacted on purpose: this string is reachable from anything
        # that renders a store, and a Postgres metadata URL carries a secret.
        self.url = engine.url.render_as_string(hide_password=True)
        self.engine = engine
        self._factory = session_factory
        self.session_id = session_id
        self.project_id = self._resolve_project(session_id)

    def _resolve_project(self, session_id: uuid.UUID) -> uuid.UUID:
        with self.scope() as db:
            row = SessionRepository(db).get(session_id)
            if row is None:
                raise SessionNotFound(str(session_id))
            return row.project_id


class ActivityCollector:
    """Records activity rows in order, with a boundary marked per completed turn.

    An `ActivityListener` (D14). Exceptions raised here would be swallowed and
    logged by `ActivityEmitter` anyway; it raises none, and it does no I/O, so
    it cannot slow a turn either.
    """

    def __init__(self) -> None:
        self.records: list[ActivityRecord] = []
        self._cursor = 0

    # -- ActivityListener --------------------------------------------------
    def on_begin(self, record: ActivityRecord) -> None:
        self.records.append(record)

    def on_end(self, record: ActivityRecord) -> None:
        self.records.append(record)

    # -- partitioning ------------------------------------------------------
    def drain(self) -> list[int]:
        """`seq` of every row appended since the previous drain."""
        fresh = self.records[self._cursor :]
        self._cursor = len(self.records)
        return [r.seq for r in fresh if r.seq]


def build_orchestrator(
    *,
    engine: Engine,
    session_factory: sessionmaker[OrmSession],
    session_id: uuid.UUID,
    client: InferenceClient,
    listeners: Sequence[ActivityListener] = (),
) -> Orchestrator:
    """One `Orchestrator` for one request, bound to one session's durable state."""
    store = ApiStore(engine, session_factory, session_id)
    return Orchestrator(client=client, store=store, listeners=listeners)


def run_chat(
    orchestrator: Orchestrator,
    utterance: str,
    *,
    collector: ActivityCollector,
) -> ChatResponse:
    """Route one utterance into a plan, execute it, and shape the whole thing for HTTP.

    The response is the plan *and* the executed prefix, never one flattened
    answer: a halted plan is a 200 whose `results` hold what did happen and
    whose `not_run` holds what did not (D15).
    """
    plan = orchestrator.plan(utterance)
    plan_seqs = collector.drain()

    results: list[DirectiveResult] = []

    def on_turn(turn: Turn) -> None:
        position = len(results) + 1
        directives = plan.directives
        results.append(
            DirectiveResult(
                position=position,
                directive=directives[position - 1] if position <= len(directives) else None,
                turn=turn_out(turn),
                activity_seqs=collector.drain(),
            )
        )

    turns = orchestrator.execute_plan(plan, utterance, on_turn=on_turn)
    # `execute_plan` publishes every turn it produces, so `results` is already
    # complete. The guard is for a future caller that does not -- a dropped
    # turn must never become a silently short response.
    if len(results) < len(turns):  # pragma: no cover - defensive
        for extra in turns[len(results) :]:
            on_turn(extra)

    status, stopped = _outcome(plan, turns)
    return ChatResponse(
        session_id=orchestrator.store.session_id,
        utterance=utterance,
        status=status,
        plan=PlanOut(
            directives=list(plan.directives),
            confidence=plan.confidence,
            clarifying_question=plan.clarifying_question,
            refusal=plan.refusal,
            activity_seqs=plan_seqs,
        ),
        results=results,
        not_run=list(plan.directives[len(results) :]),
        stopped_reason=stopped,
        activities=[ActivityOut.from_record(r) for r in collector.records],
    )


def _outcome(plan: Plan, turns: list[Turn]) -> tuple[ChatStatus, str | None]:
    """`completed` / `halted` / `refused`, and the sentence explaining a short run.

    A one-directive plan whose turn is `error` or `clarification_needed` is
    **completed**: the plan ran in full and the turn carries its own class, the
    same way layer 2 returns `clarification_needed` on a 200. `halted` is
    reserved for the case a client renders differently -- directives that were
    asked for and never ran.
    """
    if plan.refusal:
        return "refused", plan.refusal
    if len(turns) < len(plan.directives):
        last = turns[-1] if turns else None
        reason = (
            f"stopped after directive {len(turns)} of {len(plan.directives)}: "
            f"{last.intent} returned {last.kind}"
            if last is not None
            else "no directive ran"
        )
        return "halted", reason
    return "completed", None


def turn_out(turn: Turn) -> TurnOut:
    return TurnOut(
        kind=turn.kind,
        intent=turn.intent,
        text=turn.text,
        sql=turn.sql,
        ddl=turn.ddl,
        table=(
            DataTableOut(
                columns=list(turn.table.columns),
                rows=[list(row) for row in turn.table.rows],
                caption=turn.table.caption,
                truncated=turn.table.truncated,
                total_rows=turn.table.total_rows,
                row_count=turn.table.row_count,
            )
            if turn.table is not None
            else None
        ),
        attempts=list(turn.attempts),
        notes=list(turn.notes),
        deterministic_answer=turn.deterministic_answer,
        plan_position=turn.plan_position,
        plan_length=turn.plan_length,
    )
