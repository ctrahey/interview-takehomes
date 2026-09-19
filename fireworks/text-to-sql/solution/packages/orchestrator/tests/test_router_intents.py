"""The intent table, replayed against what the live model actually said (D8).

Every case here was captured from ``kimi-k2p7-code`` by
``scripts/capture_fixtures.py``. That matters: a hand-written fixture would
assert our beliefs about the router prompt, whereas these assert the router
prompt's measured behaviour. Change an utterance or the template and the fixture
key changes, the test fails with ``FixtureNotFound``, and the fix is to re-run
the capture against a live model -- which is exactly the right amount of
friction for a prompt change.
"""

from __future__ import annotations

import pytest

from t2s_nl.clients import offline_client
from t2s_nl.intents import INTENTS
from t2s_nl.router import RouterContext, route
from t2s_nl.scenarios import EMPTY_CONTEXT, LOADED_CONTEXT, ROUTER_CASES


def _context(name: str) -> RouterContext:
    return LOADED_CONTEXT if name == "loaded" else EMPTY_CONTEXT


@pytest.mark.parametrize(
    ("utterance", "context_name", "expected"),
    ROUTER_CASES,
    ids=[u[:40].replace(" ", "-") for u, _, _ in ROUTER_CASES],
)
def test_router_picks_the_right_intent(utterance: str, context_name: str, expected: str) -> None:
    decision = route(utterance, client=offline_client(), context=_context(context_name))
    assert decision.intent == expected


def test_chris_examples_resolve_to_the_right_deterministic_read() -> None:
    """The two utterances that most tempt a model to answer instead of classify."""
    databases = route("show me my databases", client=offline_client(), context=LOADED_CONTEXT)
    assert databases.intent == "inspect"
    assert databases.parameters.inspect_target == "databases"

    rows = route(
        "show me some sample rows from orders", client=offline_client(), context=LOADED_CONTEXT
    )
    assert rows.intent == "inspect"
    assert rows.parameters.inspect_target == "sample_rows"
    assert (rows.parameters.table or "").lower() == "orders"


def test_the_router_extracts_the_payload_alongside_the_intent() -> None:
    schema = route(
        "I want to model a small bookstore: authors, books, customers and orders",
        client=offline_client(),
        context=EMPTY_CONTEXT,
    )
    assert schema.intent == "create_schema"
    assert "book" in (schema.parameters.text or "").lower()

    rows = route(
        "fill the database with about 25 rows per table",
        client=offline_client(),
        context=LOADED_CONTEXT,
    )
    assert rows.intent == "load_data"
    assert rows.parameters.row_count == 25

    fix = route(
        "actually, revenue is in cents not dollars",
        client=offline_client(),
        context=LOADED_CONTEXT,
    )
    assert fix.intent == "corrective"
    assert "cent" in (fix.parameters.text or "").lower()


def test_nonsense_is_a_question_not_a_guess() -> None:
    decision = route("purple monkey dishwasher", client=offline_client(), context=EMPTY_CONTEXT)
    assert decision.intent == "unknown"
    assert decision.clarifying_question, "an unknown intent must come with something to ask"


def test_every_intent_is_exercised_by_the_table() -> None:
    covered = {expected for _, _, expected in ROUTER_CASES}
    assert covered == set(INTENTS), f"intents with no case: {set(INTENTS) - covered}"
