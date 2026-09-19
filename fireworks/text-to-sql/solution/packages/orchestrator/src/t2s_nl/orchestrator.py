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
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session as OrmSession

from foundation import ddl as ddl_module
from foundation import sample_db, security
from foundation.graph import EntityGraph
from foundation.models import DataModel, Query, Schema, SessionState
from foundation.repositories import (
    CorrectiveRepository,
    DatabaseRepository,
    DataModelRepository,
    DataModelVersionRepository,
    DatasetRepository,
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
from t2s_nl import data as data_module
from t2s_nl import inspection
from t2s_nl.activity import ActivityEmitter, ActivityListener
from t2s_nl.clients import NO_MODEL_AVAILABLE
from t2s_nl.correctives import compose_session_summary
from t2s_nl.intents import Directive, Intent, Plan
from t2s_nl.offline_router import OFFLINE_ROUTING_NOTE
from t2s_nl.router import RouterContext, no_model_reason, route
from t2s_nl.store import Store
from t2s_nl.turns import DataTable, Turn

__all__ = [
    "GENERATION_NEEDS_A_MODEL",
    "HELP_TEXT",
    "MAX_REJECTION_NOTES",
    "Orchestrator",
]

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
        """The router call, bracketed by a ``router.classify`` activity (D14)."""
        with self.activity.step(
            "router.classify",
            summary="working out what you meant",
            detail={"utterance": utterance[:200]},
        ) as step:
            plan = route(
                utterance,
                client=self.client,
                context=self._router_context(),
                on_response=lambda response: step.record_inference(
                    model=response.model,
                    tokens=response.usage.total_tokens,
                    request_id=response.request_id,
                ),
            )
            step.note(
                directives=list(plan.intents),
                confidence=plan.confidence,
                # Provenance in the append-only log, not only on screen: a
                # transcript has to show which turns the model classified and
                # which the keyword router did.
                routed_by=plan.routed_by,
            )
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
            self._note_routing(plan, refused)
            if on_turn is not None:
                on_turn(refused)
            return [refused]

        turns: list[Turn] = []
        total = len(plan.directives)
        for position, directive in enumerate(plan.directives):
            turn = self.execute(directive, utterance, clarification=plan.clarifying_question)
            turn.plan_position = position + 1
            turn.plan_length = total
            self._note_routing(plan, turn)
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
    def _note_routing(plan: Plan, turn: Turn) -> None:
        """Make keyword routing visible on every turn it produced.

        Not optional and not a debug flag. The whole justification for the
        keyword router is that the output space is a small enum; the price of
        using it is saying so, every time, so nobody reads a keyword match as
        the model's judgement.
        """
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
            detail={"description": description[:200], "iterating": bool(existing_ddl)},
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
            detail={"database": str(database_id)[:8]},
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
