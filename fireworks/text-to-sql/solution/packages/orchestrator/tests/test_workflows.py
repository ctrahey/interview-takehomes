"""The money path, driven with a scripted model so the workflow is under test.

These assertions are about *our* orchestration -- what gets persisted, which
pointers move, what the user is shown -- not about model quality. The live-model
version of the same path is ``test_money_path_offline.py``, which replays
captured fixtures.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from nl_doubles import ScriptedClient, envelope_payload, router_payload
from sqlalchemy import select

from foundation import sample_db
from foundation.models import DataModel, DataModelVersion, Dataset, Query, Schema
from foundation.repositories import SessionStateRepository
from t2s_nl.orchestrator import Orchestrator, _derive_name, _summarise_graph_warnings
from t2s_nl.store import Store

DDL = """\
CREATE TABLE authors (id INTEGER NOT NULL PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE books (
    id INTEGER NOT NULL PRIMARY KEY,
    author_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    FOREIGN KEY (author_id) REFERENCES authors (id)
);
"""

SAMPLE_DATA = {
    "tables": [
        {
            "name": "authors",
            "columns": ["id", "name"],
            "rows": [["1", "Ursula Le Guin"], ["2", "Octavia Butler"]],
        },
        {
            "name": "books",
            "columns": ["id", "author_id", "title", "price_cents"],
            "rows": [
                ["1", "1", "A Wizard of Earthsea", "1299"],
                ["2", "1", "The Dispossessed", "1499"],
                ["3", "2", "Kindred", "1199"],
            ],
        },
    ],
    "notes": "two authors, three books",
}


def make_orchestrator(store: Store, client: ScriptedClient) -> Orchestrator:
    return Orchestrator(client=client, store=store)


def test_full_path_describe_to_execute(store: Store, sample_db_dir: Path) -> None:
    client = ScriptedClient(
        router=[
            router_payload("create_schema", text="a bookstore with authors and books"),
            router_payload("load_data", row_count=3),
            router_payload("query", text="which author has the most books?"),
            router_payload("execute"),
        ],
        envelope=[
            envelope_payload("valid", query=DDL, prose="Two tables."),
            envelope_payload(
                "valid",
                query=(
                    "SELECT a.name, COUNT(*) AS n FROM authors a "
                    "JOIN books b ON b.author_id = a.id GROUP BY a.name ORDER BY n DESC"
                ),
                prose="Authors by book count.",
            ),
        ],
        data=[SAMPLE_DATA],
    )
    orch = make_orchestrator(store, client)

    # 1. describe -> DDL + entity graph, persisted and versioned
    turn = orch.handle("a bookstore with authors and books")
    assert turn.kind == "answer"
    assert turn.ddl is not None and "CREATE TABLE" in turn.ddl
    with store.scope() as db:
        models = list(db.execute(select(DataModel)).scalars())
        versions = list(db.execute(select(DataModelVersion)).scalars())
        schemas = list(db.execute(select(Schema)).scalars())
    assert len(models) == 1 and len(versions) == 1 and len(schemas) == 1
    assert versions[0].version == 1
    assert {t["name"] for t in versions[0].graph["tables"]} == {"authors", "books"}

    # 2. create a database and load generated data, validated on the way in
    turn = orch.handle("load it with data")
    assert turn.kind == "answer"
    assert turn.table is not None
    assert turn.deterministic_answer is True
    loaded = {row[0]: row[1] for row in turn.table.rows}
    assert loaded == {"authors": "2", "books": "3"}
    with store.scope() as db:
        datasets = list(db.execute(select(Dataset)).scalars())
        state = SessionStateRepository(db).get_or_create(store.session_id)
        database_id = state.current_database_id
    assert len(datasets) == 1
    assert database_id is not None and sample_db.exists(database_id)

    # 3. a question becomes SQL, persisted against the session
    turn = orch.handle("which author has the most books?")
    assert turn.sql is not None and turn.sql.upper().startswith("SELECT")
    with store.scope() as db:
        queries = list(db.execute(select(Query)).scalars())
        state = SessionStateRepository(db).get_or_create(store.session_id)
    assert len(queries) == 1
    assert state.last_query_id == queries[0].id
    assert state.last_question == "which author has the most books?"

    # 4. "run that" -- the pronoun resolves through the persisted pointer
    turn = orch.handle("run that")
    assert turn.kind == "answer"
    assert turn.table is not None
    assert turn.table.columns == ["name", "n"]
    assert turn.table.rows[0] == ["Ursula Le Guin", "2"]
    assert turn.deterministic_answer is True


def test_iteration_makes_a_new_version_of_the_same_model(store: Store) -> None:
    revised = DDL + (
        "CREATE TABLE reviews (id INTEGER NOT NULL PRIMARY KEY, book_id INTEGER NOT NULL, "
        "rating INTEGER NOT NULL, FOREIGN KEY (book_id) REFERENCES books (id));\n"
    )
    client = ScriptedClient(
        router=[
            router_payload("create_schema", text="a bookstore"),
            router_payload("create_schema", text="add a reviews table"),
        ],
        envelope=[
            envelope_payload("valid", query=DDL, prose="Two tables."),
            envelope_payload("valid", query=revised, prose="Now with reviews."),
        ],
    )
    orch = make_orchestrator(store, client)
    orch.handle("a bookstore")
    orch.handle("add a reviews table")

    with store.scope() as db:
        models = list(db.execute(select(DataModel)).scalars())
        versions = list(db.execute(select(DataModelVersion)).scalars())
    assert len(models) == 1, "iterating must version the model, not fork a new one"
    assert [v.version for v in versions] == [1, 2]

    # The revision prompt carries the previous DDL, which is how "add a table"
    # can mean "reproduce what exists, plus this".
    schema_calls = [c for c in client.calls if c.schema_name == "t2s_envelope"]
    assert "CREATE TABLE authors" in schema_calls[1].text


def test_query_without_a_schema_asks_instead_of_guessing(store: Store) -> None:
    client = ScriptedClient(router=[router_payload("query", text="how many orders?")])
    turn = make_orchestrator(store, client).handle("how many orders?")
    assert turn.kind == "clarification_needed"
    assert "schema" in turn.text.lower()
    # It never reached the generation call: only the router was consulted.
    assert [c.schema_name for c in client.calls] == ["t2s_plan"]


def test_execute_without_a_database_asks_instead_of_failing(store: Store) -> None:
    client = ScriptedClient(
        router=[
            router_payload("create_schema", text="a bookstore"),
            router_payload("query", text="list the authors"),
            router_payload("execute"),
        ],
        envelope=[
            envelope_payload("valid", query=DDL, prose="Two tables."),
            envelope_payload("valid", query="SELECT name FROM authors", prose="Authors."),
        ],
    )
    orch = make_orchestrator(store, client)
    orch.handle("a bookstore")
    orch.handle("list the authors")
    turn = orch.handle("run that")
    assert turn.kind == "clarification_needed"
    assert "load" in turn.text.lower()


def test_unknown_intent_asks_the_routers_question(store: Store) -> None:
    client = ScriptedClient(
        router=[
            router_payload(
                "unknown", clarifying_question="Did you mean the orders table or the model?"
            )
        ]
    )
    turn = make_orchestrator(store, client).handle("orders")
    assert turn.kind == "clarification_needed"
    assert turn.text == "Did you mean the orders table or the model?"


def test_clarification_from_the_core_is_a_turn_not_an_error(store: Store) -> None:
    client = ScriptedClient(
        router=[
            router_payload("create_schema", text="a bookstore"),
            router_payload("query", text="show me the best books"),
        ],
        envelope=[
            envelope_payload("valid", query=DDL, prose="Two tables."),
            envelope_payload(
                "clarification_needed", prose="Best by sales, by rating, or by price?"
            ),
        ],
    )
    orch = make_orchestrator(store, client)
    orch.handle("a bookstore")
    turn = orch.handle("show me the best books")
    assert turn.kind == "clarification_needed"
    assert turn.text.startswith("Best by")


def test_session_resumes_after_restart(store: Store, db_url: str, sample_db_dir: Path) -> None:
    """Close the chat, reopen it: the pointers, model and correctives survive."""
    client = ScriptedClient(
        router=[
            router_payload("create_schema", text="a bookstore"),
            router_payload("query", text="list the authors"),
        ],
        envelope=[
            envelope_payload("valid", query=DDL, prose="Two tables."),
            envelope_payload("valid", query="SELECT name FROM authors", prose="Authors."),
        ],
    )
    orch = make_orchestrator(store, client)
    orch.handle("a bookstore")
    orch.handle("list the authors")
    orch.add_corrective("price_cents is in cents")
    original_session = store.session_id

    # A brand new process would build a new Store over the same URL.
    reopened = Store(db_url)
    assert reopened.session_id == original_session
    resumed = Orchestrator(client=ScriptedClient(), store=reopened)

    state = resumed.state()
    values = {row[0]: row[1] for row in state.rows}
    assert values["data model"] != "-"
    assert values["version"] == "v1"
    assert values["last question"] == "list the authors"
    assert values["correctives"] == "1"
    assert resumed.current_sql() == "SELECT name FROM authors"
    assert resumed.current_version_graph() is not None


def test_sample_rows_names_the_tables_when_none_is_given(store: Store, sample_db_dir: Path) -> None:
    client = ScriptedClient(
        router=[router_payload("create_schema", text="a bookstore"), router_payload("load_data")],
        envelope=[envelope_payload("valid", query=DDL, prose="Two tables.")],
        data=[SAMPLE_DATA],
    )
    orch = make_orchestrator(store, client)
    orch.handle("a bookstore")
    orch.handle("load it")
    table = orch.catalogue("sample_rows", table=None)
    assert "authors" in (table.caption or "") and "books" in (table.caption or "")

    missing = orch.catalogue("sample_rows", table="nope")
    assert "no table named" in (missing.caption or "")


def test_ddl_with_no_tables_is_an_error_turn(store: Store) -> None:
    """A valid DDL script that defines no tables is an error, not a saved model."""
    client = ScriptedClient(
        router=[router_payload("create_schema", text="something")],
        envelope=[envelope_payload("valid", query="CREATE INDEX idx ON t (a);", prose="Hmm.")],
    )
    turn = make_orchestrator(store, client).handle("something")
    assert turn.kind == "error"
    with store.scope() as db:
        assert list(db.execute(select(DataModel)).scalars()) == []


def test_repair_attempts_survive_onto_the_turn(store: Store) -> None:
    """The rejected candidate and the engine's error must reach the renderer."""
    client = ScriptedClient(
        router=[router_payload("create_schema", text="a bookstore")],
        envelope=[envelope_payload("valid", query="this is not sql at all", prose="Hmm.")],
    )
    turn = make_orchestrator(store, client).handle("a bookstore")
    assert turn.kind == "error"
    assert len(turn.attempts) > 1, "every repair attempt must be recorded"
    assert any(a.failure_message for a in turn.attempts)


def test_database_id_is_never_client_supplied(store: Store, sample_db_dir: Path) -> None:
    """D9: the filename derives from a UUID we generate, nothing user-supplied."""
    client = ScriptedClient(
        router=[router_payload("create_schema", text="a bookstore")],
        envelope=[envelope_payload("valid", query=DDL, prose="Two tables.")],
    )
    orch = make_orchestrator(store, client)
    orch.handle("a bookstore")
    database_id = orch.create_database()
    assert isinstance(database_id, uuid.UUID)
    files = list(sample_db_dir.glob("*.sqlite3"))
    assert [f.stem for f in files] == [database_id.hex]


@pytest.mark.parametrize("utterance", ["", "   ", "\t"])
def test_empty_input_never_calls_the_model(store: Store, utterance: str) -> None:
    client = ScriptedClient()
    turn = make_orchestrator(store, client).handle(utterance)
    assert turn.kind == "clarification_needed"
    assert client.calls == []


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("I want to model a small bookstore with authors and books", "bookstore-authors-books"),
        ("I run a small climbing gym. Model members, plans and check-ins.", "climbing-gym-members"),
        ("orders", "orders"),
        ("please can you help me", "model"),
    ],
)
def test_model_names_come_from_the_users_own_nouns(description: str, expected: str) -> None:
    assert _derive_name(description) == expected


def test_graph_round_trip_warnings_collapse_when_there_are_many() -> None:
    assert _summarise_graph_warnings(["a", "b"]) == ["a", "b"]
    many = _summarise_graph_warnings([f"warning {i}" for i in range(12)])
    assert len(many) == 1
    assert "12 constructs" in many[0]
    assert "nothing is lost" in many[0]
