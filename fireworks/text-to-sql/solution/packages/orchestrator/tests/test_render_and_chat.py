"""Rendering, and the slash-command surface.

The repair loop is the most interesting behaviour in the system and it is
completely invisible unless someone prints it, so "the rejected candidate and
the engine's error reach the screen" is a test, not a hope.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nl_doubles import ScriptedClient, envelope_payload, router_payload

from t2s_core.models import Attempt
from t2s_nl.chat import _one_line, _safely, run_command
from t2s_nl.orchestrator import Orchestrator
from t2s_nl.render import render_table, render_turn, repair_lines
from t2s_nl.store import Store
from t2s_nl.turns import DataTable, Turn

DDL = "CREATE TABLE t (id INTEGER NOT NULL PRIMARY KEY, name TEXT NOT NULL);\n"


def test_a_single_successful_attempt_prints_nothing() -> None:
    assert repair_lines([Attempt(index=0, ok=True, candidate_sql="SELECT 1")]) == []


def test_the_repair_loop_shows_the_rejection_the_error_and_the_fix() -> None:
    attempts = [
        Attempt(
            index=0,
            ok=False,
            candidate_sql="SELECT signup_date FROM customers",
            failure_kind="binder",
            failure_message="no such column: signup_date",
        ),
        Attempt(index=1, ok=True, candidate_sql="SELECT signed_up FROM customers"),
    ]
    text = "\n".join(repair_lines(attempts))
    assert "attempt 0: rejected (binder)" in text
    assert "signup_date" in text
    assert "no such column: signup_date" in text
    assert "attempt 1: accepted" in text


def test_a_table_says_when_it_was_capped() -> None:
    table = DataTable(columns=["n"], rows=[[str(i)] for i in range(100)])
    lines = render_table(table, max_rows=5)
    assert len(lines) == 8  # header, rule, 5 rows, footer
    assert "showing 5 of 100" in lines[-1]


def test_a_table_says_when_the_engine_capped_it() -> None:
    table = DataTable(columns=["n"], rows=[["1"]], truncated=True)
    assert "capped by the engine" in "\n".join(render_table(table))


def test_an_empty_table_renders_its_caption() -> None:
    table = DataTable(columns=[], rows=[], caption="no sample databases yet")
    assert render_table(table) == ["  no sample databases yet"]


def test_error_and_clarification_render_as_ordinary_turns() -> None:
    assert "! boom" in render_turn(Turn.error("boom"))
    assert "? which one?" in render_turn(Turn.ask("which one?"))


def test_long_cells_are_truncated_not_wrapped() -> None:
    table = DataTable(columns=["sql"], rows=[["SELECT " + "x" * 200]])
    lines = render_table(table)
    assert all(len(line) < 120 for line in lines)


# ---------------------------------------------------------------------------
# slash commands
# ---------------------------------------------------------------------------
def test_slash_commands_cover_the_advertised_set(store: Store, sample_db_dir: Path) -> None:
    client = ScriptedClient(
        router=[router_payload("create_schema", text="a thing")],
        envelope=[envelope_payload("valid", query=DDL, prose="One table.")],
    )
    orch = Orchestrator(client=client, store=store)
    orch.handle("a thing")
    before = len(client.calls)

    for command in ["/state", "/models", "/dbs", "/schemas", "/queries", "/correctives", "/schema"]:
        result = run_command(orch, command)
        assert isinstance(result, Turn), command
        assert result.deterministic_answer is True
    assert len(client.calls) == before, "not one of those may touch a model"

    assert run_command(orch, "/quit") is None
    assert "workbench" in str(run_command(orch, "/help"))
    assert "unknown command" in str(run_command(orch, "/nonsense"))
    assert "usage" in str(run_command(orch, "/fix"))
    assert "no current SQL" in str(run_command(orch, "/sql"))


def test_slash_new_clears_the_pointers(store: Store, sample_db_dir: Path) -> None:
    client = ScriptedClient(
        router=[router_payload("create_schema", text="a thing")],
        envelope=[envelope_payload("valid", query=DDL, prose="One table.")],
    )
    orch = Orchestrator(client=client, store=store)
    orch.handle("a thing")
    assert orch.current_version_graph() is not None
    run_command(orch, "/new")
    assert orch.current_version_graph() is None
    # The model itself is still saved -- "new" forgets the pointer, not the work.
    assert orch.catalogue("models").rows


def test_the_chat_never_raises_for_an_expected_condition(store: Store) -> None:
    class Boom:
        model = "boom"

        def complete(self, *a, **k):  # type: ignore[no-untyped-def]
            raise RuntimeError("kaboom")

    orch = Orchestrator(client=Boom(), store=store)
    turns = _safely(orch, "anything")
    assert len(turns) == 1
    turn = turns[0]
    assert turn.kind == "error"
    assert "kaboom" in turn.text
    assert "session is intact" in turn.text


@pytest.mark.parametrize("command", ["/run", "/rows"])
def test_commands_needing_state_say_so_rather_than_failing(
    store: Store, sample_db_dir: Path, command: str
) -> None:
    orch = Orchestrator(client=ScriptedClient(), store=store)
    result = run_command(orch, command)
    assert result is not None
    rendered = result if isinstance(result, str) else render_turn(result)
    assert rendered.strip()


def test_scripted_mode_takes_the_same_slash_path_as_the_repl(
    store: Store, sample_db_dir: Path
) -> None:
    """`--ask "/state"` must not go through the router -- it is a slash command."""
    client = ScriptedClient(
        router=[router_payload("create_schema", text="a thing")],
        envelope=[envelope_payload("valid", query=DDL, prose="One table.")],
    )
    orch = Orchestrator(client=client, store=store)
    orch.handle("a thing")
    before = len(client.calls)

    result = _one_line(orch, "/state")
    assert isinstance(result, Turn)
    assert result.deterministic_answer is True
    assert len(client.calls) == before, "a slash command must never reach the model"
    assert _one_line(orch, "/quit") is None


def test_a_table_with_columns_but_no_rows_prints_only_its_caption() -> None:
    table = DataTable(columns=["id", "model"], rows=[], caption="no sample databases yet")
    assert render_table(table) == ["  no sample databases yet"]
