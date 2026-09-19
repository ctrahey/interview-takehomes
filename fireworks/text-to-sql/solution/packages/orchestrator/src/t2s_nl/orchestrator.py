"""Layer 3: the natural-language orchestrator (MAIN.md §"The Fully Natural-Language API").

The shape of every turn is the same, and it is the whole thesis:

    utterance → router (ONE llm call, enum-constrained) → Plan[Directive, ...]
              → for each directive, in order:
                  ├── state questions  → read foundation, render ourselves
                  ├── generation       → t2s_core, gated and repaired
                  └── execution        → foundation's sample database, D9-gated
              (each directive bracketed by begin/end activity rows, D14;
               halting on the first non-answer with the prefix still rendered)

The model picks a branch and fills in slots. It never supplies a fact. When the
user asks "show me my databases", ``inspection.list_databases`` answers out of
the metadata store and the model's contribution was the single word
``databases``. This is the only reason a three-layer split is worth its cost,
so it is enforced structurally: this module imports ``inspection`` (which has no
client) and ``t2s_core.generate_*`` (which only ever produces SQL, gated), and
there is no path by which model output becomes a reported fact.

Everything durable lives in ``foundation``: the project, the session, the data
models and their versions, the schemas, datasets, databases, queries, the
correctives, and -- since D14 -- the append-only activity log. The "current"
pointers live in ``foundation.models.SessionState``, so ``t2s-chat`` can be
closed and reopened and the pronouns still resolve.

Two additions from W13 are worth naming here because they are one mechanism:

* **Every step appends activities** (D14). ``begin`` before the work, ``end``
  after it, never a mutation. That is what a surface subscribes to in order to
  draw "⋯ generating sample data  3.4s" instead of leaving the terminal silent.
* **A directive's referent is resolved out of that log** (D15). "what's the SQL
  for that?" reads the last successful ``query.generate`` activity. No second
  model call is made to work out what "that" meant, which is exactly why the
  activity log and multi-directive plans are one unit of work and not two.

W17 adds the other end of the lifecycle, and it is the first thing here that is
irreversible, so it works differently from everything above:

* **``destroy`` and ``clear_data`` never run on the utterance that asked for
  them.** The first turn resolves *which* database, reads what is actually in it
  and returns a description plus a question; a ``PendingAction`` row carries the
  request across the turn boundary. Only an affirmative next turn executes it.
* **The answer to that question is read in code** (``t2s_nl.confirmation``), not
  classified by a model, and anything that is neither yes nor no invalidates the
  pending action and is routed normally -- so a "yes" typed after the
  conversation has moved on destroys nothing.
* **The router finally sees the conversation.** ``RouterContext`` carries the
  last few turns, reconstructed from the activity log (``t2s_nl.history``), so
  "I already mentioned it" has something to resolve against.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, cast

from sqlalchemy.orm import Session as OrmSession

from foundation import ddl as ddl_module
from foundation import paths, sample_db, security
from foundation.graph import EntityGraph
from foundation.models import DataModel, Query, Schema, SessionState
from foundation.repositories import (
    CorrectiveRepository,
    DatabaseRepository,
    DataModelRepository,
    DataModelVersionRepository,
    DatasetRepository,
    PendingActionRepository,
    QueryRepository,
    SchemaRepository,
    SessionRepository,
    SessionStateRepository,
)
from t2s_core import (
    QueryRequest,
    QueryResult,
    SchemaRequest,
    SchemaResult,
    generate_query,
    generate_schema,
)
from t2s_core.models import Attempt
from t2s_core.ports import InferenceClient
from t2s_nl import confirmation, inspection, lifecycle
from t2s_nl import data as data_module
from t2s_nl import history as history_module
from t2s_nl.activity import ActivityEmitter, ActivityListener
from t2s_nl.clients import NO_MODEL_AVAILABLE
from t2s_nl.correctives import compose_session_summary
from t2s_nl.intents import Directive, Intent, Parameters, Plan
from t2s_nl.offline_router import OFFLINE_ROUTING_NOTE
from t2s_nl.router import RouterContext, no_model_reason, route
from t2s_nl.store import Store
from t2s_nl.turns import DataTable, Turn

__all__ = [
    "CONFIRMATION_NOTE",
    "GENERATION_NEEDS_A_MODEL",
    "HELP_TEXT",
    "MAX_REJECTION_NOTES",
    "Orchestrator",
]

#: Shown on a turn that was produced by reading a yes/no answer rather than by
#: routing it. Same honesty rule as ``OFFLINE_ROUTING_NOTE``: the provenance of
#: a decision is part of the answer, and this one decided a deletion.
CONFIRMATION_NOTE = "read as an answer to my question, in code — the model was not asked"

#: What a pending confirmation's cancellation says. The user MUST be told: a
#: silently dropped confirmation makes a later "yes" a no-op with no explanation.
_CANCELLED_NOTE = (
    "I was waiting for you to confirm that I should {action} {subject}; {reason}, "
    "so I've dropped that request and nothing was deleted. Ask again if you still want it."
)


#: The verb each destructive action is described with, in one place so the
#: confirmation prompt, the log summary and the cancellation note agree.
#: Where `export` writes. Overridable so a container can aim it at a mounted
#: host directory (`T2S_EXPORT_DIR=/export`), which is what makes the copy
#: reachable from outside the container at all.
def _export_destination(database_id: uuid.UUID) -> Path:
    directory = Path(os.environ.get("T2S_EXPORT_DIR") or Path.cwd())
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{database_id.hex[:8]}.sqlite3"


_VERB: dict[str, str] = {"destroy": "destroy", "clear_data": "clear the data from"}

#: Backstop for the one boundary this package will not cross: a directive that
#: got as far as a generation call with no model behind it. The keyword router
#: refuses ``query``/``create_schema``/``corrective`` before they ever run, so
#: this is reached by ``load_data`` (whose rows come from the model) and by any
#: future path that reaches ``t2s_core`` without one. It is an explanation, not
#: an attempt: no SQL, no DDL and no rows are ever synthesised here.
GENERATION_NEEDS_A_MODEL = (
    "That step needs the language model to write something, and {reason}. "
    "I will not fabricate SQL, DDL or sample data. Set FIREWORKS_API_KEY (or "
    "put the key in ~/.fireworks-key) and run without T2S_OFFLINE, or capture a fixture "
    "for this step. Everything that reads your own saved objects still works — /state, "
    "/models, /dbs, /schema, /rows, /queries, /log."
)

#: How many per-row validation rejections to show before summarising the rest.
MAX_REJECTION_NOTES = 10

HELP_TEXT = """\
I am a text-to-SQL workbench you can talk to. What I can do, in the order it usually goes:

  describe a domain      "a bookstore with authors, books and orders"
                         → a versioned data model + DDL, saved
  iterate on it          "add a reviews table with a rating"
  create a database      "make me a sample database"
  load sample data       "load it with about 20 rows per table"
  ask questions          "which author sold the most copies last year?"
                         → SQL, bind-checked and repaired if it fails
  run them               "run that"  → rows out of the sample database
  correct me             "actually, price_cents is in cents"
                         → remembered against this model, and used from now on
  clear or delete it     "empty that database" → rows gone, database stays
                         "delete the bookstore database" → the instance is gone
                         both describe exactly what will be lost and wait for
                         you to say yes; nothing destructive happens on one line
  ask about your stuff   "show me my databases", "what's my current schema"
  ask what I'm doing     "what have you been doing?"  → the activity log, with
                         timings, so a slow turn can be attributed rather than
                         guessed at

You can ask for more than one thing at once: "load sample data and then show me
a query for unpaid balances" runs both, in order, and stops if the first fails.

Slash commands are shortcuts for the impatient -- /state /models /dbs /schema
/sql /run /log /fix <text> /correctives /help /quit. Everything they do can also be
said in plain English.

Answers about your own objects are read straight out of the database and
rendered by code; the language model classifies what you asked and writes SQL,
but never reports what exists."""


@dataclass(slots=True)
class _Pointers:
    """A snapshot of the session's current-pointers, read once per turn."""

    data_model_id: uuid.UUID | None = None
    version_id: uuid.UUID | None = None
    schema_id: uuid.UUID | None = None
    database_id: uuid.UUID | None = None
    last_query_id: uuid.UUID | None = None
    last_question: str | None = None


@dataclass(frozen=True, slots=True)
class _Pending:
    """A detached read of the session's pending destructive request (W17).

    Detached for the same reason ``ActivityRecord`` is: everything in this class
    opens short transactions, and a live ORM object outliving its scope is how
    a "still pending?" check ends up reading a stale identity map.
    """

    action: str
    database_id: uuid.UUID | None
    description: str
    detail: dict[str, Any]


class Orchestrator:
    """One conversation. Stateless in memory; all state is in ``foundation``."""

    def __init__(
        self,
        *,
        client: InferenceClient,
        store: Store | None = None,
        dialect: str = "sqlite",
        rows_per_table: int = data_module.DEFAULT_ROWS_PER_TABLE,
        listeners: Sequence[ActivityListener] = (),
    ) -> None:
        self.client = client
        self.store = store or Store()
        self.dialect = dialect
        self.rows_per_table = rows_per_table
        #: D14. Owned here because the orchestrator is what knows when a step
        #: starts and stops; surfaces subscribe rather than poll.
        self.activity = ActivityEmitter(self.store, self.store.session_id, listeners)

    # -- public surface ---------------------------------------------------
    def run(self, utterance: str, on_turn: Callable[[Turn], None] | None = None) -> list[Turn]:
        """Route one utterance into a plan and execute every directive in it (D15).

        The primary entry point. Returns one :class:`Turn` per directive that
        ran, in order. ``on_turn`` is called with each turn the moment it
        completes, so a surface renders the first half of a compound request
        while the second half is still running -- which is the difference
        between a plan and a batch.
        """
        text = utterance.strip()
        if not text:
            turns = [Turn.ask("Say something and I'll have a go.")]
        else:
            return self.execute_plan(self.plan(text), text, on_turn=on_turn)
        if on_turn is not None:
            for turn in turns:
                on_turn(turn)
        return turns

    def handle(self, utterance: str) -> Turn:
        """Run the utterance and return the final turn.

        A convenience over :meth:`run` for the single-directive case, which is
        the common one. Nothing is dropped: every directive still executes and
        every one still appends its activities; this returns the last turn
        because a caller that asked for one turn wants the outcome.
        """
        turns = self.run(utterance)
        return turns[-1] if turns else Turn.ask("Say something and I'll have a go.")

    def plan(self, utterance: str) -> Plan:
        """The router call, bracketed by a ``router.classify`` activity (D14).

        Two things happen before the model is asked, and both are deliberate.

        A pending destructive confirmation is resolved first (W17). "yes" is not
        a classification problem: making it one would let a network failure
        cancel a destruction, a mislabelled enum cause one, and would leave the
        confirmation flow broken offline. So an affirmative becomes the pending
        action's own directive, a refusal becomes ``cancel``, and *anything
        else* invalidates the pending action and falls through to ordinary
        routing -- the conservative reading, which is the only safe one here.

        The router context is then built *outside* the activity step. It now
        includes recent turns read from the log, and building it inside would
        mean this very utterance's ``begin`` row were already on disk and could
        feed itself back as its own context.
        """
        decided, plan_notes = self._pending_plan(utterance)
        context = self._router_context()
        with self.activity.step(
            "router.classify",
            summary="working out what you meant",
            detail={"utterance": utterance[:200]},
        ) as step:
            plan = (
                decided
                if decided is not None
                else route(
                    utterance,
                    client=self.client,
                    context=context,
                    on_response=lambda response: step.record_inference(
                        model=response.model,
                        tokens=response.usage.total_tokens,
                        request_id=response.request_id,
                    ),
                )
            )
            step.note(
                directives=list(plan.intents),
                confidence=plan.confidence,
                # Provenance in the append-only log, not only on screen: a
                # transcript has to show which turns the model classified and
                # which the keyword router did.
                routed_by=plan.routed_by,
            )
            plan.notes.extend(n for n in plan_notes if n not in plan.notes)
            if plan.refusal:
                # The refusal's own first sentence, not a guess at which
                # refusal it was: the log has to distinguish "too many
                # directives" from "that needs a model I do not have".
                step.refuse(f"refused: {_first_sentence(plan.refusal)}")
            else:
                step.summary = _plan_summary(plan)
        return plan

    def execute_plan(
        self,
        plan: Plan,
        utterance: str,
        *,
        on_turn: Callable[[Turn], None] | None = None,
    ) -> list[Turn]:
        """Run the directives in order, halting on the first that does not answer.

        Halting is the point of ordering them: "load sample data and then show
        me a query for unpaid balances" has no useful second half if the first
        half failed. The turns already produced are returned regardless, so the
        surface renders the completed prefix rather than throwing the whole
        exchange away.
        """
        if plan.refusal:
            # D15's cap, and the keyword router's "that needs generation".
            # Refused whole -- not truncated to the first N, which is how a
            # misreading turns into a chain of unintended actions.
            refused = Turn.error(plan.refusal, intent="unknown")
            self._note_routing(plan, refused, first=True)
            if on_turn is not None:
                on_turn(refused)
            return [refused]

        turns: list[Turn] = []
        total = len(plan.directives)
        for position, directive in enumerate(plan.directives):
            turn = self.execute(directive, utterance, clarification=plan.clarifying_question)
            turn.plan_position = position + 1
            turn.plan_length = total
            self._note_routing(plan, turn, first=position == 0)
            turns.append(turn)
            if turn.kind != "answer":
                remaining = total - position - 1
                if remaining:
                    turn.notes.append(
                        f"stopped here; {remaining} further directive(s) in that request "
                        "were not run"
                    )
                # Published *after* the "stopped here" note, so what the user
                # sees is what the log records.
                if on_turn is not None:
                    on_turn(turn)
                break
            if on_turn is not None:
                on_turn(turn)
        return turns

    @staticmethod
    def _note_routing(plan: Plan, turn: Turn, *, first: bool = False) -> None:
        """Make keyword routing visible on every turn it produced.

        Not optional and not a debug flag. The whole justification for the
        keyword router is that the output space is a small enum; the price of
        using it is saying so, every time, so nobody reads a keyword match as
        the model's judgement.

        ``plan.notes`` -- currently only "a pending destruction was cancelled
        because you moved on" (W17) -- goes on the **first** turn alone. It is
        about the utterance, not about any one directive, and repeating it under
        each half of a compound answer would read as two cancellations.
        """
        if first:
            for note in plan.notes:
                if note not in turn.notes:
                    turn.notes.append(note)
        if plan.routed_by != "keyword":
            return
        note = plan.routing_note or OFFLINE_ROUTING_NOTE
        if note not in turn.notes:
            turn.notes.append(note)

    def execute(
        self,
        directive: Directive,
        utterance: str,
        *,
        clarification: str | None = None,
    ) -> Turn:
        """Run one already-routed directive. Split out so tests can drive it directly.

        A table, not a chain of ``if``s, so that adding an intent means adding a
        row here and a handler -- and so that the set of things the router can
        cause is visible in one place. **Every directive passes exactly the gate
        it would have passed as a lone intent** (D15): there is no bulk path, no
        "we already checked the first one" shortcut, and a plan is only ever
        this method called N times.
        """
        intent: Intent = directive.intent
        params = directive.parameters
        handlers: dict[str, Callable[[], Turn]] = {
            "help": lambda: Turn(intent="help", text=HELP_TEXT, deterministic_answer=True),
            "inspect": lambda: self._inspect(directive),
            "create_schema": lambda: self._create_schema(
                params.text or utterance, name=params.model_ref
            ),
            "query": lambda: self._query(params.text or utterance),
            "load_data": lambda: self._load_data(
                rows_per_table=params.row_count or self.rows_per_table,
                seed=params.seed or data_module.DEFAULT_SEED,
            ),
            "execute": lambda: self._execute_sql(referent=directive.referent),
            "corrective": lambda: self._corrective(params.text or utterance),
            # W17. Both go through one gate, and neither can act on this turn
            # unless a pending confirmation for exactly this action already
            # exists -- which `plan()` guarantees only happens after the user
            # said yes to it.
            "destroy": lambda: self._lifecycle(directive, action="destroy"),
            "clear_data": lambda: self._lifecycle(directive, action="clear_data"),
            "export": partial(self._export, directive),
            "cancel": self._cancel_pending,
        }
        handler = handlers.get(intent)
        if handler is None:  # "unknown", and anything the enum grows later
            question = clarification or (
                "I'm not sure what you'd like me to do. You can describe a domain to model, "
                "ask a question of the data, ask about your saved objects, or say /help."
            )
            return Turn.ask(question, intent="unknown")

        try:
            return handler()
        except NO_MODEL_AVAILABLE as exc:
            # A directive that needs generation, reached with no model behind
            # it. The refusal is the product: an explanation of what is missing,
            # never a fabricated answer. See GENERATION_NEEDS_A_MODEL.
            return Turn.error(
                GENERATION_NEEDS_A_MODEL.format(reason=no_model_reason(exc)),
                intent=intent,
            )
        except (
            security.UnsafeQueryError,
            sample_db.SampleDatabaseError,
            data_module.DataGenerationError,
            ValueError,
        ) as exc:
            # Every one of these is an expected condition with a user-facing
            # message. None of them is a traceback in the chat loop.
            return Turn.error(str(exc), intent=intent)

    # -- deterministic state reads ---------------------------------------
    def _inspect(self, directive: Directive) -> Turn:
        """Answer from ``foundation``. The model chose the target and nothing else."""
        referenced = self._resolve_referent(directive)
        if referenced is not None:
            return referenced
        table = self.catalogue(
            directive.parameters.inspect_target, table=directive.parameters.table
        )
        return Turn(
            intent="inspect",
            text=table.caption or "",
            table=table,
            deterministic_answer=True,
        )

    # -- referents (D15, resolved against the D14 log) --------------------
    def _resolve_referent(self, directive: Directive) -> Turn | None:
        """Answer "what's the SQL for *that*?" out of the activity log.

        The whole mechanism: a referent names a kind of prior output, and the
        activity log already holds the last one of every kind. So "that" is a
        `SELECT ... ORDER BY seq DESC LIMIT 1`, not a second inference call and
        not a heuristic over the transcript. This is why D14 and D15 are one
        unit of work -- without the log, resolving a referent means either
        asking a model what the user meant or keeping a parallel, mutable
        "last thing" pointer per kind, and both are worse.

        Returns ``None`` when the directive has no referent, so ordinary
        inspection falls through untouched.
        """
        kind = directive.referent_kind
        if kind is None:
            return None
        record = self.activity.latest(kind)
        if record is None:
            return Turn.ask(
                _MISSING_REFERENT[directive.referent],
                intent="inspect",
            )
        detail = dict(record.detail or {})
        note = f"from activity #{record.seq} ({record.kind}, {record.at:%H:%M:%S})"
        if kind == "query.generate" and detail.get("sql"):
            return Turn(
                intent="inspect",
                sql=str(detail["sql"]),
                text=str(detail.get("question") or ""),
                notes=[note],
                deterministic_answer=True,
            )
        if kind == "schema.generate" and detail.get("ddl"):
            return Turn(
                intent="inspect",
                ddl=str(detail["ddl"]),
                notes=[note],
                deterministic_answer=True,
            )
        # last_result / last_data: the rows themselves are not kept in the log
        # (it is a journal, not a result cache), so answer with the summary the
        # step recorded and point at the deterministic read that has the rows.
        return Turn(
            intent="inspect",
            text=record.summary or record.label,
            notes=[note],
            deterministic_answer=True,
        )

    def activity_table(self, limit: int = inspection.ACTIVITY_LOG_LIMIT) -> DataTable:
        """The session's activity log, for ``/log`` (D14). No model, ever."""
        with self.store.scope() as db:
            return inspection.activity_log(db, self.store.session_id, limit=limit)

    def state(self) -> DataTable:
        with self.store.scope() as db:
            return inspection.state_summary(db, self.store.session_id)

    def catalogue(self, target: str, *, table: str | None = None) -> DataTable:
        """Every state read in the package, dispatched by name. No model, ever.

        Shared by the natural-language ``inspect`` path and the slash commands,
        precisely so the two cannot drift: "show me my databases" and ``/dbs``
        are the same function call with the same answer.
        """
        with self.store.scope() as db:
            pointers = self._pointers(db)
            readers: dict[str, Callable[[], DataTable]] = {
                "models": lambda: inspection.list_models(db, self.store.project_id),
                "databases": lambda: inspection.list_databases(db, self.store.project_id),
                "schemas": lambda: inspection.list_schemas(db, self.store.project_id),
                "queries": lambda: inspection.list_queries(db, self.store.session_id),
                "sessions": lambda: inspection.list_sessions(db, self.store.project_id),
                "correctives": lambda: inspection.list_correctives(db, pointers.data_model_id),
                "activity": lambda: inspection.activity_log(db, self.store.session_id),
                "schema_detail": lambda: inspection.schema_detail(
                    db, pointers.version_id, table=table
                ),
                "sample_rows": lambda: inspection.sample_rows(
                    db, pointers.database_id, pointers.version_id, table
                ),
            }
            reader = readers.get(target)
            if reader is None:  # "state" and "unspecified" both mean "where am I"
                return inspection.state_summary(db, self.store.session_id)
            return reader()

    # -- generation -------------------------------------------------------
    def _create_schema(self, description: str, *, name: str | None = None) -> Turn:
        """Describe a domain → DDL + entity graph, stored as a new model version.

        Iteration is the same call with the existing DDL supplied as background:
        a second description against a session that already has a model produces
        version N+1 of *that* model, not a second model. That is what makes
        "add a reviews table" work.
        """
        with self.store.scope() as db:
            pointers = self._pointers(db)
            existing_ddl = self._current_ddl(db, pointers)
            correctives = self._corrective_texts(db, pointers.data_model_id)
            current_model = (
                db.get(DataModel, pointers.data_model_id)
                if pointers.data_model_id is not None
                else None
            )
            # The name this model will have once saved, worked out *before* the
            # call so the step can record what it is about. Exact in all three
            # branches, because `_model_for_revision` applies the same
            # precedence: an explicit name, else the model being revised, else
            # one derived from the description.
            subject = (
                name or (current_model.name if current_model else None) or _derive_name(description)
            )

        background = None
        if existing_ddl:
            background = (
                "The schema currently under discussion is below. The user is asking for a "
                "revision of it, so reproduce it in full with their change applied.\n\n"
                f"```sql\n{existing_ddl}\n```"
            )
        with self.activity.step(
            "schema.generate",
            summary="designing the schema",
            detail={
                "description": description[:200],
                "iterating": bool(existing_ddl),
                history_module.SUBJECT_KEY: f"data model {subject!r}",
            },
        ) as step:
            result = generate_schema(
                SchemaRequest(
                    description=description,
                    dialect=self.dialect,  # type: ignore[arg-type]
                    session_summary=compose_session_summary(correctives, base=background),
                ),
                client=self.client,
            )
            self._record_generation(step, result, key="ddl")
        if result.response_class != "valid" or not result.query:
            return _from_envelope(result, intent="create_schema")

        parsed = ddl_module.parse_ddl(result.query, self.dialect)
        if not parsed.graph.tables:
            return Turn.error(
                "The generated DDL did not contain any tables I could parse.",
                intent="create_schema",
                ddl=result.query,
            )

        with self.store.scope() as db:
            pointers = self._pointers(db)
            model = self._model_for_revision(db, pointers, name=name, description=description)
            version = DataModelVersionRepository(db).create(model.id, parsed.graph)
            schema = SchemaRepository(db).create(version.id, self.dialect, result.query)
            SessionRepository(db).attach_data_model(self.store.session_id, model.id)
            SessionStateRepository(db).set(
                self.store.session_id,
                current_data_model_id=model.id,
                current_data_model_version_id=version.id,
                current_schema_id=schema.id,
                # A new schema version invalidates the loaded database.
                current_database_id=None,
            )
            table_names = [t.name for t in parsed.graph.tables]
            model_name = model.name or "(unnamed)"
            version_number = version.version

        notes = [
            f"saved as model {model_name!r} version {version_number} "
            f"({len(table_names)} tables: {', '.join(table_names)})",
            *_summarise_graph_warnings(parsed.warnings),
        ]
        return Turn(
            intent="create_schema",
            text=result.prose,
            ddl=result.query,
            attempts=list(result.metadata.attempts),
            notes=notes,
        )

    def _query(self, question: str) -> Turn:
        with self.store.scope() as db:
            pointers = self._pointers(db)
            schema_ddl = self._current_ddl(db, pointers)
            correctives = self._corrective_texts(db, pointers.data_model_id)
        if not schema_ddl:
            return Turn.ask(
                "I don't have a schema to write that against yet. Describe the domain you "
                "want to model, and I'll build one.",
                intent="query",
            )

        with self.activity.step(
            "query.generate",
            summary="writing the SQL",
            detail={"question": question[:200], "correctives": len(correctives)},
        ) as step:
            result = generate_query(
                QueryRequest(
                    question=question,
                    schema_ddl=schema_ddl,
                    dialect=self.dialect,  # type: ignore[arg-type]
                    # INTERIM (D13/W9): correctives ride in session_summary. See
                    # t2s_nl.correctives -- one line changes when W9 lands.
                    session_summary=compose_session_summary(correctives),
                ),
                client=self.client,
            )
            self._record_generation(step, result, key="sql")
            step.note(question=question[:200])
        self._record_repairs(result.metadata.attempts, model=result.metadata.model)

        with self.store.scope() as db:
            pointers = self._pointers(db)
            query_id: uuid.UUID | None = None
            if result.response_class == "valid" and result.query:
                row = QueryRepository(db).create(
                    result.query,
                    session_id=self.store.session_id,
                    schema_id=pointers.schema_id,
                    database_id=pointers.database_id,
                    question=question,
                    response_class=result.response_class,
                )
                query_id = row.id
            SessionStateRepository(db).set(
                self.store.session_id,
                last_question=question,
                **({"last_query_id": query_id} if query_id is not None else {}),
            )

        turn = _from_envelope(result, intent="query")
        if result.response_class == "valid" and correctives:
            turn.notes.append(f"{len(correctives)} corrective(s) applied")
        return turn

    def _load_data(self, *, rows_per_table: int, seed: int) -> Turn:
        """Create the sample database if needed, generate rows, validate, load.

        Two steps in one turn on purpose: "load it with data" means "make this
        real", and asking the user to create a database first would be
        bookkeeping we can do ourselves. Each half is still individually
        addressable -- ``create_database`` below is its own method.
        """
        with self.store.scope() as db:
            pointers = self._pointers(db)
            schema_ddl = self._current_ddl(db, pointers)
            graph = (
                DataModelVersionRepository(db).get_graph(pointers.version_id)
                if pointers.version_id is not None
                else None
            )
            correctives = self._corrective_texts(db, pointers.data_model_id)
            model_row = (
                db.get(DataModel, pointers.data_model_id)
                if pointers.data_model_id is not None
                else None
            )
            model_name = (model_row.name if model_row else None) or "(unnamed)"
        if schema_ddl is None or graph is None or pointers.schema_id is None:
            return Turn.ask(
                "There's no schema to load data into yet. Describe the domain you want to "
                "model first.",
                intent="load_data",
            )

        notes: list[str] = []
        database_id = pointers.database_id
        if database_id is None or not sample_db.exists(database_id):
            database_id = self.create_database()
            notes.append(f"created sample database {str(database_id)[:8]}")

        with self.activity.step(
            "data.generate",
            summary="generating sample data",
            detail={"rows_per_table": rows_per_table, "seed": seed},
        ) as step:
            generated = data_module.generate_rows(
                schema_ddl,
                client=self.client,
                dialect=self.dialect,
                rows_per_table=rows_per_table,
                seed=seed,
                session_summary=compose_session_summary(correctives),
            )
            validated = data_module.validate(generated, graph)
            step.note(
                tables=len(validated.rows),
                rows=sum(len(r) for r in validated.rows.values()),
                rejected=len(validated.rejections),
            )
        if not validated.rows:
            return Turn.error(
                "None of the generated rows survived validation, so nothing was loaded.",
                intent="load_data",
                notes=validated.rejections[:MAX_REJECTION_NOTES],
            )

        with self.activity.step(
            "data.load",
            summary="loading sample data",
            detail={
                "database": str(database_id)[:8],
                # Name, never the id: this reaches the router as history, and a
                # fresh UUID per run is a prompt no fixture can match twice (D8).
                history_module.SUBJECT_KEY: (f"the sample database for data model {model_name!r}"),
            },
        ) as load_step:
            inserted = sample_db.load(database_id, validated.rows)
            load_step.summary = f"loaded {inserted} rows into {str(database_id)[:8]}"
            load_step.note(inserted=inserted, tables=sorted(validated.rows))
        with self.store.scope() as db:
            pointers = self._pointers(db)
            DatasetRepository(db).create(
                pointers.version_id,  # type: ignore[arg-type]
                f"sample-seed{seed}-{rows_per_table}",
                {k: [dict(r) for r in v] for k, v in validated.rows.items()},
            )
            DatabaseRepository(db).mark_loaded(database_id)
            SessionStateRepository(db).set(self.store.session_id, current_database_id=database_id)

        counts = DataTable(
            columns=["table", "rows"],
            rows=[[t, str(len(r))] for t, r in validated.rows.items()],
            caption=f"{inserted} rows loaded into database {str(database_id)[:8]}",
        )
        notes.extend(validated.rejections[:MAX_REJECTION_NOTES])
        if len(validated.rejections) > MAX_REJECTION_NOTES:
            notes.append(
                f"...and {len(validated.rejections) - MAX_REJECTION_NOTES} more rejections"
            )
        if len(generated.notes.strip()) > _MIN_NOTE_LENGTH:
            # The model's own one-liner about the data, when it wrote a real one.
            notes.append(generated.notes.strip())
        return Turn(
            intent="load_data",
            text=f"Loaded {inserted} rows across {len(validated.rows)} tables.",
            table=counts,
            notes=notes,
            deterministic_answer=True,
        )

    def create_database(self) -> uuid.UUID:
        """Materialise the current schema as a sample SQLite database."""
        with self.store.scope() as db:
            pointers = self._pointers(db)
            if pointers.schema_id is None:
                raise ValueError("there is no current schema to build a database from")
            schema = db.get(Schema, pointers.schema_id)
            assert schema is not None
            ddl_text = schema.ddl
            schema_id = schema.id
        database_id = sample_db.create(ddl_text, dialect=self.dialect)
        with self.store.scope() as db:
            DatabaseRepository(db).create(schema_id, database_id)
            SessionStateRepository(db).set(self.store.session_id, current_database_id=database_id)
        return database_id

    # -- execution --------------------------------------------------------
    def _execute_sql(self, sql: str | None = None, *, referent: str = "none") -> Turn:
        """Run SQL. With no ``sql``, "that" is resolved -- log first, pointer second.

        Both sources agree in the ordinary case; the log is preferred because it
        is the thing a referent names (D15), and the session pointer is the
        fallback for a session that predates the log or whose last query was
        recorded before this instrumentation existed.
        """
        if sql is None and referent in ("none", "last_query"):
            record = self.activity.latest("query.generate")
            if record is not None and record.detail:
                candidate = record.detail.get("sql")
                if isinstance(candidate, str) and candidate.strip():
                    sql = candidate
        with self.store.scope() as db:
            pointers = self._pointers(db)
            if sql is None:
                if pointers.last_query_id is None:
                    return Turn.ask(
                        "There's no query to run yet -- ask me a question about the data first.",
                        intent="execute",
                    )
                row = db.get(Query, pointers.last_query_id)
                assert row is not None
                sql = row.sql
            database_id = pointers.database_id

        if database_id is None or not sample_db.exists(database_id):
            return Turn.ask(
                "There's no loaded sample database to run against. Say 'load it with data' "
                "and I'll create one and fill it.",
                intent="execute",
                sql=sql,
            )

        try:
            with self.activity.step(
                "query.execute",
                summary="running the query",
                detail={"database": str(database_id)[:8]},
            ) as step:
                result = sample_db.query(database_id, sql)
                step.summary = f"{result.row_count} row(s) returned"
                step.note(
                    row_count=result.row_count,
                    columns=list(result.columns),
                    truncated=result.truncated,
                )
        except security.CatalogAccessDeniedError as exc:
            # D12: the refusal is deliberate and points at the answer that IS
            # available deterministically, rather than returning an empty result.
            return Turn.error(
                "That query reads the SQLite catalogue, which is denied on sample "
                "databases (D12). Ask me what your schema looks like instead -- I answer "
                "that from the stored model, not by interrogating the database.",
                intent="execute",
                sql=sql,
                notes=[str(exc)],
            )
        except security.UnsafeQueryError as exc:
            return Turn.error(f"The safety gate refused that SQL: {exc}", intent="execute", sql=sql)
        except sample_db.QueryTimeoutError as exc:
            return Turn.error(str(exc), intent="execute", sql=sql)

        table = DataTable(
            columns=list(result.columns),
            rows=[[("NULL" if v is None else str(v)) for v in row] for row in result.rows],
            caption=f"{result.row_count} row(s)",
            truncated=result.truncated,
        )
        return Turn(
            intent="execute",
            text=f"{result.row_count} row(s).",
            table=table,
            sql=sql,
            deterministic_answer=True,
        )

    def run_current_sql(self) -> Turn:
        return self._execute_sql()

    # -- correctives ------------------------------------------------------
    def _corrective(self, text: str) -> Turn:
        with self.store.scope() as db:
            pointers = self._pointers(db)
            if pointers.data_model_id is None:
                return Turn.ask(
                    "Correctives attach to a data model, and there isn't one yet. Describe "
                    "the domain you want to model first.",
                    intent="corrective",
                )
            last_question = pointers.last_question
            data_model_id = pointers.data_model_id

        with self.activity.step("corrective.record", summary=f"recording: {text[:80]}") as step:
            with self.store.scope() as db:
                CorrectiveRepository(db).add(data_model_id, text, session_id=self.store.session_id)
                count = len(CorrectiveRepository(db).list_active(data_model_id))
            step.note(text=text[:400], active_correctives=count)

        notes = [f"{count} corrective(s) now apply to this model"]
        if last_question:
            turn = self._query(last_question)
            turn.intent = "corrective"
            turn.notes = notes + [f"re-asked: {last_question}"] + turn.notes
            return turn
        return Turn(
            intent="corrective",
            text=f"Noted, and remembered against this model: {text}",
            notes=notes,
            deterministic_answer=True,
        )

    def add_corrective(self, text: str) -> Turn:
        return self._corrective(text)

    # -- the destructive lifecycle (W17) ----------------------------------
    #
    # One gate, two actions, and a single invariant that makes the gate real:
    #
    #     a destructive handler acts ONLY when a PendingAction for exactly that
    #     action already exists, and `plan()` lets one survive into `execute()`
    #     only when the user's own next utterance was an affirmative.
    #
    # It is structural rather than a flag on the directive. A flag would be a
    # field on a model the router fills in, and "the model may not set this one
    # field" is a weaker guarantee than "the permission lives in a different
    # table, written by us, in the previous turn".

    def _lifecycle(self, directive: Directive, *, action: str) -> Turn:
        """Either perform a confirmed destruction, or describe one and ask."""
        pending = self._read_pending()
        if pending is not None and pending.action == action:
            return self._perform(pending)
        if pending is not None:  # pragma: no cover - `plan()` clears these first
            self._drop_pending("a different destructive request replaced it")
        return self._request_confirmation(directive, action=action)

    def _request_confirmation(self, directive: Directive, *, action: str) -> Turn:
        """Turn one: say exactly what would be lost, record the request, do nothing.

        "Exactly" is the whole job. "Delete the database?" is not a confirmation
        anybody can give meaningfully; "database 3f2a91b4, built from data model
        'sports-league', 142 rows across 5 tables" is. The counts are read from
        the file itself, not from what we believe we loaded.
        """
        intent = cast("Intent", action)
        named = (directive.parameters.model_ref or "").strip() or None
        with self.store.scope() as db:
            pointers = self._pointers(db)
            resolution = lifecycle.resolve(
                db,
                self.store.project_id,
                named=named,
                current_database_id=pointers.database_id,
            )

        if isinstance(resolution, lifecycle.NotFound):
            return Turn.ask(
                _no_target_message(resolution, action),
                intent=intent,
                table=_options_table(resolution.available),
            )
        if isinstance(resolution, lifecycle.Ambiguous):
            return Turn.ask(
                f"I could {_VERB[action]} more than one thing there, and I won't guess which "
                f"— {_VERB[action]} is not undoable. Which of these did you mean? Say the "
                "model's name.",
                intent=intent,
                table=_options_table(resolution.options),
            )

        target = resolution
        table, total = lifecycle.describe(target)
        description = _describe_consequence(action, target, total)
        with (
            self.activity.step(
                "confirm.request",
                summary=f"awaiting your confirmation to {_VERB[action]} {target.short_id}",
                detail={
                    "action": action,
                    "database": str(target.database_id),
                    "model": target.model_name,
                    "rows": total,
                    "tables": list(target.tables),
                    history_module.SUBJECT_KEY: target.label,
                },
            ) as step,
            self.store.scope() as db,
        ):
            PendingActionRepository(db).request(
                self.store.session_id,
                action=action,
                description=description,
                database_id=target.database_id,
                detail={
                    "model": target.model_name,
                    "rows": total,
                    history_module.SUBJECT_KEY: target.label,
                },
                requested_seq=step.begin_seq,
            )
        return Turn.ask(
            f"{description} Nothing has been changed yet — reply "
            f"{confirmation.AFFIRMATIVE_EXAMPLES} and I'll do it; say anything else and "
            "I'll leave it alone.",
            intent=intent,
            table=table,
        )

    def _perform(self, pending: _Pending) -> Turn:
        """Turn two: the user said yes. Consume the permission, then act.

        The pending row is cleared **first**. A destruction that dies halfway
        must not leave a live "yes" behind it that a later utterance could
        re-trigger; making the user ask again is the cheap failure.
        """
        action = pending.action
        intent = cast("Intent", action)
        subject = pending.detail.get(history_module.SUBJECT_KEY)
        with self.store.scope() as db:
            PendingActionRepository(db).clear(self.store.session_id)
        self.activity.note(
            "confirm.resolve",
            summary=f"you confirmed: {_VERB[action]} {subject or 'it'}",
            detail={
                "action": action,
                "outcome": "confirmed",
                history_module.SUBJECT_KEY: subject,
            },
        )

        if pending.database_id is None:  # pragma: no cover - never written null
            return Turn.error(
                "I've lost track of which database that was. Ask again and I'll re-check.",
                intent=intent,
            )
        with self.store.scope() as db:
            target = next(
                (
                    t
                    for t in lifecycle.candidates(db, self.store.project_id)
                    if t.database_id == pending.database_id
                ),
                None,
            )
        if target is None:
            return Turn.error(
                "That sample database is already gone, so there was nothing to do.",
                intent=intent,
            )
        return self._do_destroy(target) if action == "destroy" else self._do_clear(target)

    def _do_destroy(self, target: lifecycle.Target) -> Turn:
        """Delete the instance. The `Database` row survives, marked destroyed.

        Deliberately not a row deletion: the metadata store is the record that
        this database existed and was destroyed, and `destroyed_at` is only
        meaningful if the row outlives the file. The data model, its versions
        and its schema are untouched -- the user can build another database from
        the same schema, which is exactly why `destroy` and `create_schema` are
        different intents.
        """
        with self.activity.step(
            "db.destroy",
            summary=f"destroying database {target.short_id}",
            detail={
                "database": str(target.database_id),
                "model": target.model_name,
                history_module.SUBJECT_KEY: target.label,
            },
        ) as step:
            sample_db.destroy(target.database_id)
            with self.store.scope() as db:
                DatabaseRepository(db).mark_destroyed(target.database_id)
                if self._pointers(db).database_id == target.database_id:
                    SessionStateRepository(db).set(self.store.session_id, current_database_id=None)
            step.summary = f"destroyed database {target.short_id} ({target.model_name})"
        return Turn(
            intent="destroy",
            text=(
                f"Destroyed sample database {target.short_id}, built from data model "
                f"{target.model_name!r}."
            ),
            notes=[
                f"the data model {target.model_name!r} and its schema are untouched — "
                "say 'load it with data' to build a fresh database from the same schema",
            ],
            deterministic_answer=True,
        )

    def _do_clear(self, target: lifecycle.Target) -> Turn:
        """Empty the rows and leave everything else standing."""
        with self.activity.step(
            "db.clear",
            summary=f"clearing database {target.short_id}",
            detail={
                "database": str(target.database_id),
                "model": target.model_name,
                history_module.SUBJECT_KEY: target.label,
            },
        ) as step:
            deleted = sample_db.clear(target.database_id, target.tables)
            with self.store.scope() as db:
                DatabaseRepository(db).mark_empty(target.database_id)
            step.summary = f"cleared {deleted} row(s) from {target.short_id}"
            step.note(deleted=deleted)
        table, _ = lifecycle.describe(target)
        return Turn(
            intent="clear_data",
            text=(
                f"Deleted {deleted} row(s) from sample database {target.short_id}. The "
                "database and its tables are still there, empty."
            ),
            table=table,
            notes=["say 'load it with data' to fill it again"],
            deterministic_answer=True,
        )

    def _export(self, directive: Directive) -> Turn:
        """Save a copy of the sample database where the user can open it themselves.

        Deliberately not destructive and deliberately not clever: it copies, it
        says where, and it stops. The value of this turn is that everything
        *after* it happens in tools this system does not control -- DB Browser,
        the sqlite3 shell, anything -- which is the only kind of check that can
        corroborate our own answers.

        `VACUUM INTO` rather than a file copy: the snapshot goes through SQLite,
        so a database mid-write cannot produce a torn file. The source is opened
        read-only; exporting can never be what changed the data.
        """
        with self.store.scope() as db:
            pointers = self._pointers(db)
            database_id = pointers.database_id
        if database_id is None:
            return Turn.ask(
                "There's no sample database in this session yet to copy. "
                "Create one and load some data first.",
                intent="export",
            )

        destination = _export_destination(database_id)
        if destination.exists():
            destination.unlink()
        source = paths.database_path(database_id)
        if not source.exists():
            return Turn.ask("That sample database has no file on disk any more.", intent="export")

        with self.activity.step(
            "database.export",
            summary="saving a copy you can open yourself",
            detail={"database_id": str(database_id)},
        ) as step:
            connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
            try:
                connection.execute("VACUUM INTO ?", (str(destination),))
            finally:
                connection.close()
            step.note(path=str(destination), bytes=destination.stat().st_size)

        return Turn(
            intent="export",
            text=f"Saved a copy to {destination}",
            notes=[
                f"{destination.stat().st_size} bytes, a plain SQLite file",
                "open it with DB Browser, the sqlite3 shell, or anything else -- "
                "nothing downstream of that file needs this workbench",
            ],
            deterministic_answer=True,
        )

    def _cancel_pending(self) -> Turn:
        """The user said no."""
        pending = self._drop_pending("you said no")
        if pending is None:
            return Turn.ask(
                "There's nothing waiting on a yes or no from me right now.", intent="cancel"
            )
        subject = pending.detail.get(history_module.SUBJECT_KEY) or "it"
        return Turn(
            intent="cancel",
            text=f"Left alone — nothing was deleted from {subject}.",
            deterministic_answer=True,
        )

    def invalidate_pending(self, reason: str) -> str | None:
        """Cancel any pending confirmation. Returns a line to show, or ``None``.

        Public because the surfaces have doors the router never sees: ``/run``,
        ``/fix`` and ``/new`` all *do* something without going through
        :meth:`plan`, and a pending destruction that survived one of those would
        be answerable by a "yes" the user no longer means.
        """
        pending = self._drop_pending(reason)
        if pending is None:
            return None
        return _CANCELLED_NOTE.format(
            action=_VERB[pending.action],
            subject=pending.detail.get(history_module.SUBJECT_KEY) or "that database",
            reason=reason,
        )

    # -- pending-confirmation plumbing ------------------------------------
    def _pending_plan(self, utterance: str) -> tuple[Plan | None, list[str]]:
        """Read this utterance as an answer to a pending confirmation, if there is one.

        Returns the plan to run instead of routing (or ``None`` to route
        normally) and any notes the user must see regardless.

        The third branch is the one that matters. "Anything else" is not treated
        as a maybe: it invalidates. Chris's own transcript is the argument —
        three consecutive turns drifted further from the original request, and a
        "yes" at the end of that drift must not delete something nobody is
        thinking about any more.
        """
        pending = self._read_pending()
        if pending is None:
            return None, []
        verdict = confirmation.read(utterance)
        if verdict == "affirmative":
            return (
                Plan(
                    directives=[
                        Directive(
                            intent=cast("Intent", pending.action),
                            parameters=Parameters(model_ref=pending.detail.get("model")),
                            rationale="you confirmed the pending request",
                        )
                    ],
                    confidence="high",
                    routed_by="keyword",
                    routing_note=CONFIRMATION_NOTE,
                ),
                [],
            )
        if verdict == "negative":
            return (
                Plan(
                    directives=[
                        Directive(intent="cancel", rationale="you declined the pending request")
                    ],
                    confidence="high",
                    routed_by="keyword",
                    routing_note=CONFIRMATION_NOTE,
                ),
                [],
            )
        note = self.invalidate_pending("you asked for something else instead")
        return None, [note] if note else []

    def _read_pending(self) -> _Pending | None:
        with self.store.scope() as db:
            row = PendingActionRepository(db).get(self.store.session_id)
            if row is None:
                return None
            return _Pending(
                action=row.action,
                database_id=row.database_id,
                description=row.description,
                detail=dict(row.detail or {}),
            )

    def _drop_pending(self, reason: str) -> _Pending | None:
        """Clear the pending action and record *why* in the log (D14)."""
        pending = self._read_pending()
        if pending is None:
            return None
        with self.store.scope() as db:
            PendingActionRepository(db).clear(self.store.session_id)
        self.activity.note(
            "confirm.resolve",
            summary=f"not confirmed: {reason}",
            status="refused",
            detail={
                "action": pending.action,
                "outcome": "cancelled",
                "reason": reason,
                history_module.SUBJECT_KEY: pending.detail.get(history_module.SUBJECT_KEY),
            },
        )
        return pending

    # -- internals --------------------------------------------------------
    def _record_generation(
        self, step: Any, result: QueryResult | SchemaResult, *, key: str
    ) -> None:
        """Attach a generation's provenance and outcome to its activity row."""
        step.record_inference(
            model=result.metadata.model, tokens=result.metadata.usage.total_tokens
        )
        step.note(
            response_class=result.response_class,
            attempts=len(result.metadata.attempts),
            repairs_used=result.metadata.repairs_used,
        )
        if result.response_class == "valid" and result.query:
            step.note(**{key: result.query})
        else:
            step.status = "refused" if result.response_class == "clarification_needed" else "error"
            step.summary = f"{result.response_class}: {(result.prose or '')[:120]}"

    def _record_repairs(self, attempts: list[Attempt], *, model: str) -> None:
        """One ``query.repair`` pair per rejected candidate (D14).

        The repair loop lives inside ``t2s_core`` behind the ``QueryValidator``
        port (D4), so we learn about each attempt only from the result metadata
        afterwards. The durations written here are the per-attempt latencies the
        core measured -- reconstructed, never invented -- which keeps the log
        one shape rather than growing a third phase meaning "atomic".
        """
        for attempt in attempts:
            if attempt.ok:
                continue
            self.activity.note(
                "query.repair",
                summary=f"attempt {attempt.index} rejected: {attempt.failure_kind}",
                status="error",
                duration_ms=attempt.latency_ms or None,
                model=model,
                tokens=attempt.usage.total_tokens or None,
                detail={
                    "attempt": attempt.index,
                    "failure_kind": attempt.failure_kind,
                    "failure_message": (attempt.failure_message or "")[:400],
                    "candidate_sql": (attempt.candidate_sql or "")[:1000],
                },
            )

    def _router_context(self) -> RouterContext:
        with self.store.scope() as db:
            pointers = self._pointers(db)
            model = (
                db.get(DataModel, pointers.data_model_id)
                if pointers.data_model_id is not None
                else None
            )
            tables: tuple[str, ...] = ()
            if pointers.version_id is not None:
                graph = DataModelVersionRepository(db).get_graph(pointers.version_id)
                tables = tuple(t.name for t in graph.tables)
            loaded = pointers.database_id is not None and sample_db.exists(pointers.database_id)
            correctives = self._corrective_texts(db, pointers.data_model_id)
            return RouterContext(
                has_data_model=pointers.data_model_id is not None,
                data_model_name=model.name if model else None,
                table_names=tables,
                has_schema=pointers.schema_id is not None,
                has_database=pointers.database_id is not None,
                database_loaded=loaded,
                has_last_query=pointers.last_query_id is not None,
                last_question=pointers.last_question,
                corrective_count=len(correctives),
                # W17. Read out of the D14 log rather than kept alongside it --
                # the log already has the utterance, the intents it produced and
                # (since W17) the object each step touched, and a second
                # transcript would be a second thing to keep true.
                recent=history_module.recent_turns(
                    self.activity.history(limit=history_module.SCAN_ROWS)
                ),
            )

    def _pointers(self, db: OrmSession) -> _Pointers:
        state: SessionState = SessionStateRepository(db).get_or_create(self.store.session_id)
        return _Pointers(
            data_model_id=state.current_data_model_id,
            version_id=state.current_data_model_version_id,
            schema_id=state.current_schema_id,
            database_id=state.current_database_id,
            last_query_id=state.last_query_id,
            last_question=state.last_question,
        )

    @staticmethod
    def _current_ddl(db: OrmSession, pointers: _Pointers) -> str | None:
        if pointers.schema_id is None:
            return None
        schema = db.get(Schema, pointers.schema_id)
        return schema.ddl if schema is not None else None

    @staticmethod
    def _corrective_texts(db: OrmSession, data_model_id: uuid.UUID | None) -> list[str]:
        if data_model_id is None:
            return []
        return [c.text for c in CorrectiveRepository(db).list_active(data_model_id)]

    def _model_for_revision(
        self,
        db: OrmSession,
        pointers: _Pointers,
        *,
        name: str | None,
        description: str,
    ) -> DataModel:
        """The current model if we're iterating on one, otherwise a new model."""
        if pointers.data_model_id is not None:
            model = db.get(DataModel, pointers.data_model_id)
            if model is not None:
                if name and not model.name:
                    model.name = name
                return model
        return DataModelRepository(db).create(
            project_id=self.store.project_id, name=name or _derive_name(description)
        )

    def current_version_graph(self) -> EntityGraph | None:
        with self.store.scope() as db:
            pointers = self._pointers(db)
            if pointers.version_id is None:
                return None
            return DataModelVersionRepository(db).get_graph(pointers.version_id)

    def current_sql(self) -> str | None:
        with self.store.scope() as db:
            pointers = self._pointers(db)
            if pointers.last_query_id is None:
                return None
            row = db.get(Query, pointers.last_query_id)
            return row.sql if row else None

    def start_new_model(self) -> None:
        """Forget the current model so the next description starts a fresh one."""
        with self.store.scope() as db:
            SessionStateRepository(db).set(
                self.store.session_id,
                current_data_model_id=None,
                current_data_model_version_id=None,
                current_schema_id=None,
                current_database_id=None,
                last_query_id=None,
                last_question=None,
            )


#: What to say when a referent names a kind of output that has never happened.
_MISSING_REFERENT: dict[str, str] = {
    "last_query": (
        "There's no SQL yet for me to point at -- ask me a question about the data first."
    ),
    "last_result": "Nothing has been run yet, so there are no results to refer back to.",
    "last_schema": "No schema has been designed yet. Describe the domain you want to model.",
    "last_data": "No sample data has been loaded yet.",
    "none": "I'm not sure what you're referring to.",
}


#: Longest refusal fragment to put in an activity summary before truncating.
_SUMMARY_CHARS = 72


def _describe_consequence(action: str, target: lifecycle.Target, total_rows: int) -> str:
    """One sentence naming what is lost and what survives.

    Both halves are load-bearing. The user who said "clear out the sports league
    database? just totally delete it" was holding both readings at once, so the
    reply has to draw the line rather than assume they already had.
    """
    tables = len(target.tables)
    if action == "destroy":
        return (
            f"That would permanently delete sample database {target.short_id}, built from "
            f"data model {target.model_name!r} — {total_rows} row(s) across {tables} "
            "table(s), and the tables themselves. The data model and its schema would "
            "survive; only this database instance goes."
        )
    return (
        f"That would delete all {total_rows} row(s) from sample database "
        f"{target.short_id} (data model {target.model_name!r}), across {tables} table(s). "
        "The database itself, its tables and its schema all survive, and it can be "
        "loaded again."
    )


def _options_table(targets: Sequence[lifecycle.Target]) -> DataTable | None:
    if not targets:
        return None
    return DataTable(
        columns=["id", "data model", "status"],
        rows=[[t.short_id, t.model_name, t.status] for t in targets],
        caption="sample databases in this project",
    )


def _no_target_message(resolution: lifecycle.NotFound, action: str) -> str:
    if not resolution.available:
        return (
            "There are no sample databases in this project, so there is nothing to "
            f"{_VERB[action]}."
        )
    if resolution.named:
        return (
            f"I couldn't find a sample database matching {resolution.named!r}. These are "
            "the ones you have — say which, by its model's name or its id."
        )
    return (
        f"Which sample database should I {_VERB[action]}? I won't pick one for you when "
        "the answer isn't reversible."
    )


def _plan_summary(plan: Plan) -> str:
    """The one-line description of a plan that goes in its activity row.

    Carries the provenance, because a ``/log`` that shows "understood: inspect"
    for a keyword match and for a model classification alike is a log that
    cannot answer "who decided that?".
    """
    if not plan.directives:
        return "no directives"
    suffix = " (keyword)" if plan.routed_by == "keyword" else ""
    return f"understood: {' → '.join(plan.intents)}{suffix}"


def _first_sentence(text: str) -> str:
    head = text.strip().split(". ")[0].splitlines()[0]
    return head if len(head) <= _SUMMARY_CHARS else head[: _SUMMARY_CHARS - 1] + "…"


def _from_envelope(result: QueryResult | SchemaResult, *, intent: Intent) -> Turn:
    """Map a ``t2s_core`` envelope onto a conversational turn.

    ``clarification_needed`` and ``error`` are ordinary turns. The repair
    attempts ride along on every one of them -- rejected candidates are the most
    interesting thing the system does and they are invisible unless rendered.
    """
    response_class = result.response_class
    query = result.query
    prose = result.prose
    attempts = list(result.metadata.attempts)

    if response_class == "valid":
        turn = Turn(intent=intent, text=prose, attempts=attempts)
        if intent == "create_schema":
            turn.ddl = query
        else:
            turn.sql = query
        return turn
    if response_class == "clarification_needed":
        return Turn(kind="clarification_needed", intent=intent, text=prose, attempts=attempts)
    detail = (result.error.message if result.error else "") or prose or "I could not answer that."
    return Turn(kind="error", intent=intent, text=detail, attempts=attempts)


#: Words that carry no domain meaning in "I want to model a small bookstore".
#: A stopword list, not a model call: naming is bookkeeping, and spending an
#: inference round trip (plus its failure modes) on a label would be silly.
_NAME_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "for",
        "with",
        "to",
        "in",
        "on",
        "my",
        "our",
        "your",
        "i",
        "we",
        "me",
        "us",
        "you",
        "this",
        "that",
        "some",
        "any",
        "new",
        "can",
        "could",
        "would",
        "should",
        "will",
        "please",
        "help",
        "small",
        "tiny",
        "simple",
        "little",
        "basic",
        "quick",
        "want",
        "wants",
        "need",
        "needs",
        "like",
        "build",
        "building",
        "create",
        "creating",
        "make",
        "making",
        "model",
        "modeling",
        "modelling",
        "design",
        "designing",
        "schema",
        "database",
        "db",
        "data",
        "set",
        "up",
        "run",
        "runs",
        "running",
        "have",
        "has",
        "get",
        "add",
        "adding",
        "track",
        "tracking",
        "manage",
        "managing",
        "store",
        "storing",
        "system",
        "app",
        "application",
    }
)

#: How many graph-round-trip warnings to print before summarising.
_MAX_GRAPH_WARNINGS = 3

#: Shortest model "note" worth repeating to the user; below this it is filler.
_MIN_NOTE_LENGTH = 8


def _derive_name(description: str) -> str:
    """A short, stable handle from the user's own words. No model call."""
    words = "".join(c if c.isalnum() else " " for c in description).split()
    meaningful = [w.lower() for w in words if w.lower() not in _NAME_STOPWORDS and not w.isdigit()]
    return "-".join(meaningful[:3]) or "model"


def _summarise_graph_warnings(warnings: list[str]) -> list[str]:
    """Say that the graph is lossy, once, instead of once per column.

    ``parse_ddl`` emits a warning per construct the dialect-neutral graph does
    not model (AUTOINCREMENT, CHECK constraints, indexes). Every one of them is
    true and none of them is alarming: the *Schema* stores the generated DDL
    verbatim and the sample database is built from that, so nothing the user can
    see is actually lost. A dozen lines of it after every schema turn buries the
    line that matters, so past a handful they collapse into one sentence that
    says what is really going on.
    """
    if len(warnings) <= _MAX_GRAPH_WARNINGS:
        return warnings
    return [
        f"{len(warnings)} constructs (indexes, AUTOINCREMENT, CHECK constraints) are not "
        "modelled in the dialect-neutral entity graph; the DDL above is stored verbatim "
        "and the sample database is built from it, so nothing is lost"
    ]
