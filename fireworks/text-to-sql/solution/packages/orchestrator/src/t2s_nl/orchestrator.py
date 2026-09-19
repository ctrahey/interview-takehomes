"""Layer 3: the natural-language orchestrator (MAIN.md §"The Fully Natural-Language API").

The shape of every turn is the same, and it is the whole thesis:

    utterance → router (ONE llm call, enum-constrained)
              → deterministic handler
                  ├── state questions  → read foundation, render ourselves
                  ├── generation       → t2s_core, gated and repaired
                  └── execution        → foundation's sample database, D9-gated

The model picks a branch and fills in slots. It never supplies a fact. When the
user asks "show me my databases", ``inspection.list_databases`` answers out of
the metadata store and the model's contribution was the single word
``databases``. This is the only reason a three-layer split is worth its cost,
so it is enforced structurally: this module imports ``inspection`` (which has no
client) and ``t2s_core.generate_*`` (which only ever produces SQL, gated), and
there is no path by which model output becomes a reported fact.

Everything durable lives in ``foundation``: the project, the session, the data
models and their versions, the schemas, datasets, databases, queries, and the
correctives. The "current" pointers live in ``foundation.models.SessionState``,
so ``t2s-chat`` can be closed and reopened and the pronouns still resolve.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

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
from t2s_core.ports import InferenceClient
from t2s_nl import data as data_module
from t2s_nl import inspection
from t2s_nl.correctives import compose_session_summary
from t2s_nl.intents import Intent, IntentDecision
from t2s_nl.router import RouterContext, route
from t2s_nl.store import Store
from t2s_nl.turns import DataTable, Turn

__all__ = ["HELP_TEXT", "MAX_REJECTION_NOTES", "Orchestrator"]

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

Slash commands are shortcuts for the impatient -- /state /models /dbs /schema
/sql /run /fix <text> /correctives /help /quit. Everything they do can also be
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
    ) -> None:
        self.client = client
        self.store = store or Store()
        self.dialect = dialect
        self.rows_per_table = rows_per_table

    # -- public surface ---------------------------------------------------
    def handle(self, utterance: str) -> Turn:
        """Route one utterance and execute the resulting intent."""
        text = utterance.strip()
        if not text:
            return Turn.ask("Say something and I'll have a go.")

        decision = route(text, client=self.client, context=self._router_context())
        return self.execute(decision, text)

    def execute(self, decision: IntentDecision, utterance: str) -> Turn:
        """Run an already-routed decision. Split out so tests can drive it directly.

        A table, not a chain of ``if``s, so that adding an intent means adding a
        row here and a handler -- and so that the set of things the router can
        cause is visible in one place.
        """
        intent: Intent = decision.intent
        params = decision.parameters
        handlers: dict[str, Callable[[], Turn]] = {
            "help": lambda: Turn(intent="help", text=HELP_TEXT, deterministic_answer=True),
            "inspect": lambda: self._inspect(decision),
            "create_schema": lambda: self._create_schema(
                params.text or utterance, name=params.model_ref
            ),
            "query": lambda: self._query(params.text or utterance),
            "load_data": lambda: self._load_data(
                rows_per_table=params.row_count or self.rows_per_table,
                seed=params.seed or data_module.DEFAULT_SEED,
            ),
            "execute": self._execute_sql,
            "corrective": lambda: self._corrective(params.text or utterance),
        }
        handler = handlers.get(intent)
        if handler is None:  # "unknown", and anything the enum grows later
            question = decision.clarifying_question or (
                "I'm not sure what you'd like me to do. You can describe a domain to model, "
                "ask a question of the data, ask about your saved objects, or say /help."
            )
            return Turn.ask(question, intent="unknown")

        try:
            return handler()
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
    def _inspect(self, decision: IntentDecision) -> Turn:
        """Answer from ``foundation``. The model chose the target and nothing else."""
        table = self.catalogue(decision.parameters.inspect_target, table=decision.parameters.table)
        return Turn(
            intent="inspect",
            text=table.caption or "",
            table=table,
            deterministic_answer=True,
        )

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
        result = generate_schema(
            SchemaRequest(
                description=description,
                dialect=self.dialect,  # type: ignore[arg-type]
                session_summary=compose_session_summary(correctives, base=background),
            ),
            client=self.client,
        )
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

        generated = data_module.generate_rows(
            schema_ddl,
            client=self.client,
            dialect=self.dialect,
            rows_per_table=rows_per_table,
            seed=seed,
            session_summary=compose_session_summary(correctives),
        )
        validated = data_module.validate(generated, graph)
        if not validated.rows:
            return Turn.error(
                "None of the generated rows survived validation, so nothing was loaded.",
                intent="load_data",
                notes=validated.rejections[:MAX_REJECTION_NOTES],
            )

        inserted = sample_db.load(database_id, validated.rows)
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
    def _execute_sql(self, sql: str | None = None) -> Turn:
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
            result = sample_db.query(database_id, sql)
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
            CorrectiveRepository(db).add(
                pointers.data_model_id, text, session_id=self.store.session_id
            )
            count = len(CorrectiveRepository(db).list_active(pointers.data_model_id))
            last_question = pointers.last_question

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
