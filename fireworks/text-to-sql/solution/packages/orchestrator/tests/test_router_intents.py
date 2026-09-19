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
def test_router_picks_the_right_plan(
    utterance: str, context_name: str, expected: tuple[str, ...]
) -> None:
    plan = route(utterance, client=offline_client(), context=_context(context_name))
    assert plan.intents == expected


def test_chris_examples_resolve_to_the_right_deterministic_read() -> None:
    """The two utterances that most tempt a model to answer instead of classify."""
    databases = route("show me my databases", client=offline_client(), context=LOADED_CONTEXT)
    assert databases.primary.intent == "inspect"
    assert databases.primary.parameters.inspect_target == "databases"

    rows = route(
        "show me some sample rows from orders", client=offline_client(), context=LOADED_CONTEXT
    )
    assert rows.primary.intent == "inspect"
    assert rows.primary.parameters.inspect_target == "sample_rows"
    assert (rows.primary.parameters.table or "").lower() == "orders"


def test_the_router_extracts_the_payload_alongside_the_intent() -> None:
    schema = route(
        "I want to model a small bookstore: authors, books, customers and orders",
        client=offline_client(),
        context=EMPTY_CONTEXT,
    )
    assert schema.primary.intent == "create_schema"
    assert "book" in (schema.primary.parameters.text or "").lower()

    rows = route(
        "fill the database with about 25 rows per table",
        client=offline_client(),
        context=LOADED_CONTEXT,
    )
    assert rows.primary.intent == "load_data"
    assert rows.primary.parameters.row_count == 25

    fix = route(
        "actually, revenue is in cents not dollars",
        client=offline_client(),
        context=LOADED_CONTEXT,
    )
    assert fix.primary.intent == "corrective"
    assert "cent" in (fix.primary.parameters.text or "").lower()


def test_nonsense_is_a_question_not_a_guess() -> None:
    plan = route("purple monkey dishwasher", client=offline_client(), context=EMPTY_CONTEXT)
    assert plan.intents == ("unknown",)
    assert plan.clarifying_question, "an unknown intent must come with something to ask"


def test_every_intent_is_exercised_by_the_table() -> None:
    covered = {intent for _, _, expected in ROUTER_CASES for intent in expected}
    assert covered == set(INTENTS), f"intents with no case: {set(INTENTS) - covered}"


def test_the_compound_utterances_that_used_to_be_truncated() -> None:
    """D15's three examples, in Chris's words, replayed against the live model.

    The first two were silently cut in half by the single-intent router. The
    third is the one that proves a referent is not an extra step: "what's the
    SQL for that?" is ONE directive that names prior output.
    """
    both = route(
        "show me the query for unpaid balances and sample results",
        client=offline_client(),
        context=LOADED_CONTEXT,
    )
    assert both.intents == ("query", "execute")

    ordered = route(
        "populate sample data and then show me a query for unpaid balances",
        client=offline_client(),
        context=LOADED_CONTEXT,
    )
    assert ordered.intents == ("load_data", "query")

    referent = route(
        "Awesome -- what's the SQL for that?",
        client=offline_client(),
        context=LOADED_CONTEXT,
    )
    assert referent.intents == ("inspect",)
    assert referent.primary.referent == "last_query"
    assert referent.primary.referent_kind == "query.generate"
