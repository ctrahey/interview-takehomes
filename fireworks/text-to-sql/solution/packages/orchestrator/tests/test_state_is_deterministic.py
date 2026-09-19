"""The rule this whole package exists to enforce, under test.

    The LLM classifies and generates. It NEVER reports system state.

A regression here would be silent and catastrophic: a plausible-looking chat
that confidently names databases that do not exist. So the assertions below are
deliberately blunt and several of them are structural rather than behavioural --
a behavioural test can be satisfied by a coincidence, but a module that cannot
import an inference client cannot report state no matter what anyone does to it
later.
"""

from __future__ import annotations

import ast
import inspect as py_inspect
import uuid
from pathlib import Path

import pytest
from nl_doubles import (
    ExplodingClient,
    ScriptedClient,
    directive_payload,
    envelope_payload,
    router_payload,
)

from t2s_nl import inspection
from t2s_nl.intents import Directive
from t2s_nl.orchestrator import Orchestrator
from t2s_nl.store import Store

DDL = "CREATE TABLE customers (id INTEGER NOT NULL PRIMARY KEY, name TEXT NOT NULL, city TEXT);\n"

#: A name that exists only in the model's mouth. If it ever reaches the user,
#: the model reported state.
POISON = "TOTALLY_FAKE_DATABASE_9999"


def _poisoned_router(intent: str, **kwargs: object) -> dict[str, object]:
    """A router response whose free-text fields are full of invented state."""
    payload = router_payload(intent, **kwargs)  # type: ignore[arg-type]
    payload["rationale"] = (
        f"the user has three databases: {POISON}, {POISON}_b and {POISON}_c, "
        "with 4,912 rows in orders"
    )
    payload["clarifying_question"] = None
    return payload


# ---------------------------------------------------------------------------
# Structural: the answering module cannot reach a model
# ---------------------------------------------------------------------------
def test_inspection_module_imports_no_inference_client() -> None:
    source = Path(inspection.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{a.name}" for a in node.names)

    forbidden = [name for name in imported if "t2s_core" in name or "client" in name.lower()]
    assert forbidden == [], (
        "t2s_nl.inspection answers questions about the user's own objects and must "
        f"have no way to consult a model; it imports {forbidden}"
    )


def test_inspection_functions_take_no_client_parameter() -> None:
    public = [
        getattr(inspection, name)
        for name in inspection.__all__
        if callable(getattr(inspection, name))
    ]
    assert public, "sanity: inspection must export functions"
    for function in public:
        params = py_inspect.signature(function).parameters
        assert "client" not in params, f"{function.__name__} must not accept a client"


# ---------------------------------------------------------------------------
# Behavioural: the answer comes from foundation, and contradicts the model
# ---------------------------------------------------------------------------
def test_show_me_my_databases_is_answered_from_foundation(
    store: Store, sample_db_dir: Path
) -> None:
    client = ScriptedClient(
        router=[
            router_payload("create_schema", text="a tiny shop"),
            _poisoned_router("inspect", inspect_target="databases"),
        ],
        envelope=[envelope_payload("valid", query=DDL, prose="One table.")],
    )
    orch = Orchestrator(client=client, store=store)
    orch.handle("a tiny shop")
    database_id = orch.create_database()

    turn = orch.handle("show me my databases")

    assert turn.deterministic_answer is True
    assert turn.table is not None
    ids = [row[0] for row in turn.table.rows]
    assert ids == [str(database_id)[:8]], "the answer must be the real database, from foundation"

    rendered = "\n".join([turn.text or "", *[" ".join(r) for r in turn.table.rows], *turn.notes])
    assert POISON not in rendered, "model-invented state leaked into the answer"


def test_the_model_is_asked_exactly_once_and_told_nothing_it_could_parrot(
    store: Store, sample_db_dir: Path
) -> None:
    client = ScriptedClient(
        router=[
            router_payload("create_schema", text="a tiny shop"),
            router_payload("inspect", inspect_target="databases"),
        ],
        envelope=[envelope_payload("valid", query=DDL, prose="One table.")],
    )
    orch = Orchestrator(client=client, store=store)
    orch.handle("a tiny shop")
    database_id = orch.create_database()

    before = len(client.calls)
    turn = orch.handle("show me my databases")
    inspect_calls = client.calls[before:]

    assert [c.schema_name for c in inspect_calls] == ["t2s_plan"], (
        "an inspect turn is one routing call and then deterministic code"
    )
    sent = inspect_calls[0].text
    assert str(database_id) not in sent
    assert str(database_id)[:8] not in sent
    assert str(store.session_id) not in sent
    assert turn.table is not None and turn.table.rows


def test_state_answers_survive_the_model_being_taken_away(
    store: Store, sample_db_dir: Path
) -> None:
    """Once the intent is known, nothing else needs a model -- prove it by removing it.

    The decision is routed normally, then the client is replaced with one that
    raises on contact. If executing an ``inspect`` decision still produces the
    right answer, no part of that answer could have come from a model.
    """
    client = ScriptedClient(
        router=[router_payload("create_schema", text="a tiny shop")],
        envelope=[envelope_payload("valid", query=DDL, prose="One table.")],
    )
    orch = Orchestrator(client=client, store=store)
    orch.handle("a tiny shop")

    directive = Directive.model_validate(
        directive_payload("inspect", inspect_target="schema_detail")
    )
    orch.client = ExplodingClient()
    turn = orch.execute(directive, "what's my current schema")

    assert turn.deterministic_answer is True
    assert turn.table is not None
    assert [r[1] for r in turn.table.rows] == ["id", "name", "city"]

    # ...and the exploding client really does explode, so the test has teeth.
    with pytest.raises(AssertionError):
        orch.client.complete([], response_schema={}, schema_name="t2s_plan")


def test_slash_commands_never_call_a_model(store: Store, sample_db_dir: Path) -> None:
    client = ScriptedClient(
        router=[router_payload("create_schema", text="a tiny shop")],
        envelope=[envelope_payload("valid", query=DDL, prose="One table.")],
    )
    orch = Orchestrator(client=client, store=store)
    orch.handle("a tiny shop")
    orch.create_database()
    before = len(client.calls)

    for target in ("models", "databases", "schemas", "queries", "correctives", "state"):
        table = orch.catalogue(target)
        assert table is not None
    assert orch.state() is not None
    assert len(client.calls) == before, "slash commands must be model-free"


def test_row_counts_and_columns_come_from_the_database_not_the_model(
    store: Store, sample_db_dir: Path
) -> None:
    data = {
        "tables": [
            {
                "name": "customers",
                "columns": ["id", "name", "city"],
                "rows": [["1", "Ada", "London"], ["2", "Grace", "New York"]],
            }
        ],
        "notes": "two customers",
    }
    client = ScriptedClient(
        router=[
            router_payload("create_schema", text="a tiny shop"),
            router_payload("load_data"),
            _poisoned_router("inspect", inspect_target="sample_rows", table="customers"),
        ],
        envelope=[envelope_payload("valid", query=DDL, prose="One table.")],
        data=[data],
    )
    orch = Orchestrator(client=client, store=store)
    orch.handle("a tiny shop")
    orch.handle("load it with data")

    turn = orch.handle("show me some sample rows from customers")
    assert turn.table is not None
    assert turn.table.columns == ["id", "name", "city"]
    assert [r[1] for r in turn.table.rows] == ["Ada", "Grace"]
    assert POISON not in " ".join(" ".join(r) for r in turn.table.rows)


def test_inspecting_an_unknown_table_says_so_rather_than_inventing_one(
    store: Store, sample_db_dir: Path
) -> None:
    client = ScriptedClient(
        router=[router_payload("create_schema", text="a tiny shop")],
        envelope=[envelope_payload("valid", query=DDL, prose="One table.")],
    )
    orch = Orchestrator(client=client, store=store)
    orch.handle("a tiny shop")
    table = orch.catalogue("schema_detail", table="orders")
    assert table.rows == []
    assert "no table named 'orders'" in (table.caption or "")
    assert "customers" in (table.caption or "")


def test_empty_catalogue_says_empty(store: Store) -> None:
    orch = Orchestrator(client=ScriptedClient(), store=store)
    assert orch.catalogue("models").rows == []
    assert "no data models yet" in (orch.catalogue("models").caption or "")
    assert "no sample databases yet" in (orch.catalogue("databases").caption or "")


def test_state_summary_reports_only_what_is_set(store: Store) -> None:
    orch = Orchestrator(client=ScriptedClient(), store=store)
    values = {row[0]: row[1] for row in orch.state().rows}
    assert values["data model"] == "-"
    assert values["sample database"] == "-"
    assert values["correctives"] == "0"
    assert uuid.UUID(values["session"]) == store.session_id
