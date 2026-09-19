"""The router degrades; it does not crash.

inference-findings: enforced structured output guarantees shape, never meaning,
and a truncated response is invalid JSON rather than a schema violation
(finding #1). All of that has to land on "ask the user", because a chat loop that
raises on a bad completion is a chat loop that dies mid-conversation.
"""

from __future__ import annotations

import json

import pytest
from nl_doubles import ScriptedClient, router_payload

from t2s_core.errors import UpstreamError
from t2s_core.ports import InferenceResponse
from t2s_nl.intents import INTENTS, ROUTER_SCHEMA, decision_from_payload
from t2s_nl.router import RouterContext, route


class _RawClient:
    """Returns whatever bytes it was given, bypassing the payload helpers."""

    def __init__(self, content: str) -> None:
        self.content = content

    model = "raw/test"

    def complete(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        return InferenceResponse(content=self.content, model=self.model, finish_reason="stop")


class _FailingClient:
    model = "failing/test"

    def complete(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        raise UpstreamError("503 from upstream")


def test_the_wire_schema_pins_the_enum() -> None:
    assert ROUTER_SCHEMA["properties"]["intent"]["enum"] == list(INTENTS)
    assert ROUTER_SCHEMA["additionalProperties"] is False
    assert set(ROUTER_SCHEMA["required"]) == set(ROUTER_SCHEMA["properties"])


@pytest.mark.parametrize(
    "content",
    [
        '{"response_class": "valid", "query": "SELECT',  # truncated mid-string
        "not json at all",
        "[]",
        '{"intent": "DEFINITELY_NOT_AN_INTENT", "confidence": "high"}',
        '{"confidence": "high"}',
    ],
)
def test_unusable_router_output_becomes_a_question(content: str) -> None:
    decision = route("do something", client=_RawClient(content), context=RouterContext())
    assert decision.intent == "unknown"
    assert decision.clarifying_question


def test_a_valid_intent_with_broken_parameters_keeps_the_intent() -> None:
    payload = router_payload("query", text="how many?")
    payload["parameters"] = {"row_count": "not a number", "inspect_target": 17}
    decision = decision_from_payload(json.dumps(payload))
    assert decision.intent == "query"
    assert decision.confidence == "low"
    assert decision.parameters.row_count is None


def test_an_out_of_range_row_count_is_discarded_not_honoured() -> None:
    payload = router_payload("load_data", row_count=999_999_999)
    decision = decision_from_payload(json.dumps(payload))
    assert decision.intent == "load_data"
    assert decision.parameters.row_count is None


def test_transport_failure_is_a_question_not_a_traceback() -> None:
    decision = route("anything", client=_FailingClient(), context=RouterContext())
    assert decision.intent == "unknown"
    assert "503" in (decision.clarifying_question or "")
    assert "/state" in (decision.clarifying_question or "")


def test_the_router_is_told_what_exists_but_not_what_is_in_it() -> None:
    client = ScriptedClient(router=[router_payload("execute")])
    context = RouterContext(
        has_data_model=True,
        data_model_name="bookstore",
        table_names=("authors", "books"),
        has_database=True,
        database_loaded=True,
        has_last_query=True,
        last_question="which author sold the most?",
        corrective_count=2,
    )
    route("run that", client=client, context=context)

    sent = client.calls[0].text
    # Presence and identifiers -- yes, pronouns need them.
    assert "authors, books" in sent
    assert "a sample database exists: yes" in sent
    # Contents -- no. Nothing here is a row, a count of records, or an id.
    assert "uuid" not in sent.lower()
    assert "never answer their question" in sent


def test_an_irrelevant_bad_parameter_does_not_cost_us_the_good_ones() -> None:
    """Observed live: a create_schema decision arriving with ``row_count: 0``.

    Models fill irrelevant optional fields with placeholder zeros. Discarding
    the whole parameter block there threw away the schema description, which is
    the one field that turn actually needed.
    """
    payload = router_payload("create_schema", text="a small bookstore")
    payload["parameters"]["row_count"] = 0  # type: ignore[index]
    decision = decision_from_payload(json.dumps(payload))

    assert decision.intent == "create_schema"
    assert decision.parameters.text == "a small bookstore"
    assert decision.parameters.row_count is None
    assert decision.confidence == "low", "pruning is a degradation and must say so"
