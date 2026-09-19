"""The money path, replayed end to end against real captured model output.

    describe a domain → DDL + entity graph (stored, versioned) → sample database
    → generate and load sample data → ask a question → SQL → execute → rows

``test_workflows.py`` tests the same path with a scripted model, which proves
the orchestration. This one proves the *integration*: real DDL from a real
model, parsed into a real entity graph, rendered into a real SQLite file, loaded
with real generated rows, queried by real generated SQL. Nothing is stubbed
except the network.

Because the conversation is replayed from fixtures keyed on the full request,
this test also silently asserts that the prompts are deterministic: any change
to a template, to the router's state checklist, or to the corrective carrier
changes a key and the replay fails loudly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from t2s_nl.clients import offline_client
from t2s_nl.orchestrator import Orchestrator
from t2s_nl.scenarios import MONEY_PATH
from t2s_nl.store import Store
from t2s_nl.turns import Turn


@pytest.fixture
def replayed(store: Store, sample_db_dir: Path) -> list[tuple[str, Turn]]:
    orch = Orchestrator(client=offline_client(), store=store)
    return [(utterance, orch.handle(utterance)) for utterance in MONEY_PATH]


def _turn(replayed: list[tuple[str, Turn]], index: int) -> Turn:
    return replayed[index][1]


def test_no_turn_in_the_money_path_is_an_error(replayed: list[tuple[str, Turn]]) -> None:
    failures = [(u, t.kind, t.text) for u, t in replayed if t.kind == "error"]
    assert failures == []


def test_describe_produces_a_versioned_model_with_real_tables(
    replayed: list[tuple[str, Turn]],
) -> None:
    turn = _turn(replayed, 0)
    assert turn.intent == "create_schema"
    assert turn.ddl and "CREATE TABLE" in turn.ddl.upper()
    statements = [s for s in sqlglot.parse(turn.ddl, read="sqlite") if s is not None]
    assert len(statements) >= 4
    assert all(isinstance(s, exp.Create) for s in statements)
    assert any("saved as model" in note for note in turn.notes)


def test_the_schema_read_back_comes_from_the_stored_graph(
    replayed: list[tuple[str, Turn]],
) -> None:
    turn = _turn(replayed, 1)
    assert turn.intent == "inspect"
    assert turn.deterministic_answer is True
    assert turn.table is not None
    assert turn.table.columns == ["table", "column", "type", "null", "key"]
    tables = {row[0] for row in turn.table.rows}
    assert "authors" in tables and "books" in tables
    assert any(row[4].startswith("FK") for row in turn.table.rows), "FKs survived the round trip"


def test_sample_data_is_generated_validated_and_loaded(
    replayed: list[tuple[str, Turn]],
) -> None:
    turn = _turn(replayed, 2)
    assert turn.intent == "load_data"
    assert turn.table is not None
    counts = {row[0]: int(row[1]) for row in turn.table.rows}
    assert len(counts) >= 4
    assert all(n > 0 for n in counts.values())
    assert "Loaded" in turn.text


def test_sample_rows_come_out_of_the_database(replayed: list[tuple[str, Turn]]) -> None:
    turn = _turn(replayed, 3)
    assert turn.intent == "inspect"
    assert turn.deterministic_answer is True
    assert turn.table is not None and turn.table.rows
    assert "title" in [c.lower() for c in turn.table.columns]


def test_a_question_becomes_a_single_read_only_select(
    replayed: list[tuple[str, Turn]],
) -> None:
    turn = _turn(replayed, 4)
    assert turn.intent == "query"
    assert turn.sql is not None
    parsed = [s for s in sqlglot.parse(turn.sql, read="sqlite") if s is not None]
    assert len(parsed) == 1
    assert isinstance(parsed[0], exp.Select | exp.Subquery)


def test_execute_returns_real_rows(replayed: list[tuple[str, Turn]]) -> None:
    turn = _turn(replayed, 5)
    assert turn.intent == "execute"
    assert turn.deterministic_answer is True
    assert turn.table is not None
    assert turn.table.rows, "the generated query returned nothing against the generated data"


def test_a_corrective_reaches_the_next_generation(replayed: list[tuple[str, Turn]]) -> None:
    turn = _turn(replayed, 6)
    assert turn.intent == "corrective"
    assert any("corrective" in note for note in turn.notes)
    assert any("re-asked" in note for note in turn.notes)


def test_the_final_state_question_is_answered_from_foundation(
    replayed: list[tuple[str, Turn]],
) -> None:
    turn = _turn(replayed, 7)
    assert turn.intent == "inspect"
    assert turn.deterministic_answer is True
    assert turn.table is not None
    assert turn.table.columns[0] == "id"
    assert len(turn.table.rows) == 1, "exactly one sample database was created"
    assert turn.table.rows[0][4] == "loaded"
    assert turn.table.rows[0][5] == "yes"


def test_the_whole_conversation_resumes_after_a_restart(
    store: Store, db_url: str, sample_db_dir: Path
) -> None:
    orch = Orchestrator(client=offline_client(), store=store)
    for utterance in MONEY_PATH:
        orch.handle(utterance)

    # A second process opening the same store.
    resumed = Orchestrator(client=offline_client(), store=Store(db_url))
    state = {row[0]: row[1] for row in resumed.state().rows}
    assert state["data model"] != "-"
    assert state["version"] == "v1"
    assert state["sample database"].startswith("loaded")
    assert state["correctives"] == "1"
    assert resumed.current_sql() is not None

    # And "run that" still works, with no model call at all.
    turn = resumed.run_current_sql()
    assert turn.kind == "answer"
    assert turn.table is not None and turn.table.rows
