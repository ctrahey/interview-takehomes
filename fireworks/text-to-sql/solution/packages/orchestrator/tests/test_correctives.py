"""Correctives: accepted in conversation, persisted per data model, reused (D13).

The product loop D13 describes: an answer is wrong for a reason no schema can
express, the user says why, and from then on every generation knows. The proof
that it works is not that the corrective is stored -- it is that the *next*
prompt contains it and survives a restart.
"""

from __future__ import annotations

from pathlib import Path

from nl_doubles import ScriptedClient, envelope_payload, router_payload
from sqlalchemy import select

from foundation.models import DataModel
from foundation.repositories import CorrectiveRepository
from t2s_nl.orchestrator import Orchestrator
from t2s_nl.store import Store

DDL = (
    "CREATE TABLE orders (id INTEGER NOT NULL PRIMARY KEY, revenue_cents INTEGER NOT NULL, "
    "status TEXT NOT NULL);\n"
)


def _model_with_a_question(store: Store) -> tuple[Orchestrator, ScriptedClient]:
    client = ScriptedClient(
        router=[
            router_payload("create_schema", text="orders with revenue and status"),
            router_payload("query", text="what is total revenue?"),
        ],
        envelope=[
            envelope_payload("valid", query=DDL, prose="One table."),
            envelope_payload("valid", query="SELECT SUM(revenue_cents) FROM orders", prose="."),
        ],
    )
    orch = Orchestrator(client=client, store=store)
    orch.handle("orders with revenue and status")
    orch.handle("what is total revenue?")
    return orch, client


def test_a_corrective_is_persisted_against_the_data_model(store: Store) -> None:
    orch, _ = _model_with_a_question(store)
    orch.add_corrective("revenue_cents is in cents, divide by 100 for dollars")

    with store.scope() as db:
        model = db.execute(select(DataModel)).scalars().one()
        stored = CorrectiveRepository(db).list_active(model.id)
    assert [c.text for c in stored] == ["revenue_cents is in cents, divide by 100 for dollars"]


def test_a_corrective_re_asks_the_last_question_and_reaches_the_next_prompt(
    store: Store,
) -> None:
    orch, client = _model_with_a_question(store)
    before = len(client.calls)

    turn = orch.add_corrective("cancelled orders have status 'C' and must be excluded")

    # The last question was re-asked automatically -- that is the demo loop.
    assert any("re-asked" in note for note in turn.notes)
    new_generation = [c for c in client.calls[before:] if c.schema_name == "t2s_envelope"]
    assert new_generation, "the corrective must trigger a fresh generation"
    assert "status 'C'" in new_generation[-1].text
    assert any("corrective(s) applied" in note for note in turn.notes)


def test_correctives_accumulate_and_all_reach_the_prompt(store: Store) -> None:
    orch, client = _model_with_a_question(store)
    orch.add_corrective("revenue_cents is in cents")
    orch.add_corrective("cancelled orders have status 'C'")
    generation = [c for c in client.calls if c.schema_name == "t2s_envelope"][-1]
    assert "revenue_cents is in cents" in generation.text
    assert "cancelled orders have status 'C'" in generation.text


def test_correctives_survive_a_restart_and_apply_to_a_later_question(
    store: Store, db_url: str, sample_db_dir: Path
) -> None:
    orch, _ = _model_with_a_question(store)
    orch.add_corrective("revenue_cents is in cents")

    # New process, new Store, same file.
    reopened = Store(db_url)
    later_client = ScriptedClient(
        router=[router_payload("query", text="what is average revenue?")],
        envelope=[
            envelope_payload("valid", query="SELECT AVG(revenue_cents) FROM orders", prose=".")
        ],
    )
    resumed = Orchestrator(client=later_client, store=reopened)
    turn = resumed.handle("what is average revenue?")

    assert turn.kind == "answer"
    generation = [c for c in later_client.calls if c.schema_name == "t2s_envelope"][-1]
    assert "revenue_cents is in cents" in generation.text, (
        "a corrective recorded before the restart must still shape generation after it"
    )
    assert any("1 corrective(s) applied" in n for n in turn.notes)


def test_a_corrective_without_a_model_asks_rather_than_dropping_it(store: Store) -> None:
    orch = Orchestrator(client=ScriptedClient(), store=store)
    turn = orch.add_corrective("revenue is in cents")
    assert turn.kind == "clarification_needed"
    assert "data model" in turn.text


def test_correctives_are_listed_deterministically(store: Store) -> None:
    orch, _ = _model_with_a_question(store)
    orch.add_corrective("revenue_cents is in cents")
    orch.add_corrective("status 'C' means cancelled")
    table = orch.catalogue("correctives")
    assert [r[1] for r in table.rows] == [
        "revenue_cents is in cents",
        "status 'C' means cancelled",
    ]
