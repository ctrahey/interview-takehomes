"""The repair loop, and the unification of its three failure classes.

`invalid_json`, `envelope_invariant`, `safety_gate` and `binder` are four
different things that go wrong, fed back to the model through one template and
one code path. These tests pin that: same loop, same attempt trace, different
`failure_kind`.
"""

from __future__ import annotations

import pytest
from doubles import ScriptedClient, envelope

from t2s_core import NoOpValidator, QueryRequest, generate_query
from t2s_core.generate import generate_schema
from t2s_core.models import SchemaRequest


def request(ddl: str, *, attempts: int = 2) -> QueryRequest:
    return QueryRequest(
        question="How many orders has each customer placed?",
        schema_ddl=ddl,
        dialect="sqlite",
        max_repair_attempts=attempts,
    )


def test_binder_error_is_repaired_and_both_attempts_are_recorded(retail_ddl: str) -> None:
    """The headline behaviour: attempt 1 invents a column, attempt 2 fixes it."""
    client = ScriptedClient(
        [
            envelope("valid", "SELECT name, email FROM customers", "Lists customers."),
            envelope("valid", "SELECT name FROM customers", "Lists customer names."),
        ]
    )
    result = generate_query(request(retail_ddl), client=client)

    assert result.response_class == "valid"
    assert result.query == "SELECT name FROM customers"

    attempts = result.metadata.attempts
    assert len(attempts) == 2
    assert attempts[0].ok is False
    assert attempts[0].failure_kind == "binder"
    assert attempts[0].candidate_sql == "SELECT name, email FROM customers"
    assert "no such column: email" in (attempts[0].failure_message or "")
    assert attempts[1].ok is True
    assert result.metadata.repairs_used == 1
    assert result.metadata.usage.total_tokens == 60

    # The repair turn must actually carry the failing SQL and the engine's words.
    repair_turn = client.calls[1][-1]
    assert repair_turn.role == "user"
    assert "SELECT name, email FROM customers" in repair_turn.content
    assert "no such column: email" in repair_turn.content
    # ...and the rejected answer is in the transcript as the assistant turn.
    assert client.calls[1][-2].role == "assistant"


def test_envelope_invariant_violation_is_repaired_like_a_binder_error(retail_ddl: str) -> None:
    """Finding #2: response_class 'error' with error: null is schema-valid and
    semantically wrong. It costs one repair turn, not an exception."""
    client = ScriptedClient(
        [
            envelope("error", None, "Something went wrong.", None),
            envelope("valid", "SELECT name FROM customers", "Lists customer names."),
        ]
    )
    result = generate_query(request(retail_ddl), client=client)

    assert result.response_class == "valid"
    assert result.metadata.attempts[0].failure_kind == "envelope_invariant"
    assert "error" in (result.metadata.attempts[0].failure_message or "")
    assert "envelope_invariant" in client.calls[1][-1].content


def test_invalid_json_is_its_own_failure_kind(retail_ddl: str) -> None:
    client = ScriptedClient(
        [
            '{"response_class": "valid", "query": "SELECT',
            envelope("valid", "SELECT name FROM customers", "Lists customer names."),
        ]
    )
    result = generate_query(request(retail_ddl), client=client)
    assert result.response_class == "valid"
    assert result.metadata.attempts[0].failure_kind == "invalid_json"


def test_safety_violation_is_repaired(retail_ddl: str) -> None:
    client = ScriptedClient(
        [
            envelope("valid", "SELECT name FROM customers; DROP TABLE customers", "Oops."),
            envelope("valid", "SELECT name FROM customers", "Lists customer names."),
        ]
    )
    result = generate_query(request(retail_ddl), client=client)
    assert result.response_class == "valid"
    assert result.metadata.attempts[0].failure_kind == "safety_gate"


def test_clarification_needed_is_terminal_and_not_repaired(retail_ddl: str) -> None:
    client = ScriptedClient([envelope("clarification_needed", None, "Which orders?")])
    result = generate_query(request(retail_ddl), client=client)
    assert result.response_class == "clarification_needed"
    assert result.query is None
    assert result.prose == "Which orders?"
    assert len(result.metadata.attempts) == 1
    assert result.metadata.attempts[0].ok is True


def test_error_class_is_terminal(retail_ddl: str) -> None:
    client = ScriptedClient(
        [
            envelope(
                "error",
                None,
                "This schema has no employees.",
                {"code": "unanswerable", "message": "No employee data.", "details": None},
            )
        ]
    )
    result = generate_query(request(retail_ddl), client=client)
    assert result.response_class == "error"
    assert result.error is not None
    assert result.error.code == "unanswerable"
    assert len(result.metadata.attempts) == 1


def test_exhausting_the_budget_yields_an_error_envelope_with_the_full_trace(
    retail_ddl: str,
) -> None:
    bad = envelope("valid", "SELECT email FROM customers", "Lists emails.")
    client = ScriptedClient([bad, bad, bad])
    result = generate_query(request(retail_ddl, attempts=2), client=client)

    assert result.response_class == "error"
    assert result.error is not None
    assert result.error.code == "repair_exhausted.binder"
    assert len(result.metadata.attempts) == 3
    assert all(a.ok is False for a in result.metadata.attempts)
    assert "no such column: email" in result.prose


def test_zero_repair_attempts_makes_one_call(retail_ddl: str) -> None:
    bad = envelope("valid", "SELECT email FROM customers", "Lists emails.")
    client = ScriptedClient([bad])
    result = generate_query(request(retail_ddl, attempts=0), client=client)
    assert result.response_class == "error"
    assert len(result.metadata.attempts) == 1
    assert len(client.calls) == 1


def test_noop_validator_disables_the_loop(retail_ddl: str) -> None:
    """The eval's loop-off arm: the same broken SQL is returned as 'valid'."""
    bad = envelope("valid", "SELECT email FROM customers", "Lists emails.")
    client = ScriptedClient([bad, bad, bad])
    result = generate_query(request(retail_ddl), client=client, validator=NoOpValidator())

    assert result.response_class == "valid"
    assert result.query == "SELECT email FROM customers"
    assert len(result.metadata.attempts) == 1
    assert result.metadata.validator == "noop"
    # ...but the envelope invariants are still enforced. The loop-off arm
    # measures the *validator's* value, not the absence of all checking.
    client = ScriptedClient(
        [envelope("valid", None, "No query here."), envelope("valid", "SELECT 1", "One.")]
    )
    repaired = generate_query(request(retail_ddl), client=client, validator=NoOpValidator())
    assert repaired.response_class == "valid"
    assert repaired.metadata.attempts[0].failure_kind == "envelope_invariant"


def test_unparseable_request_ddl_costs_zero_model_calls() -> None:
    """D9: supplied DDL is parsed before it is ever interpolated into a prompt."""
    client = ScriptedClient([])
    result = generate_query(
        QueryRequest(question="anything", schema_ddl="CREATE TABLE ((((", dialect="sqlite"),
        client=client,
    )
    assert result.response_class == "error"
    assert result.error is not None
    assert result.error.code == "invalid_schema_ddl"
    assert client.calls == []


def test_session_summary_is_included_as_background(retail_ddl: str) -> None:
    client = ScriptedClient([envelope("valid", "SELECT 1", "One.")])
    generate_query(
        QueryRequest(
            question="and for last month?",
            schema_ddl=retail_ddl,
            session_summary="The user was looking at orders in Berlin.",
        ),
        client=client,
    )
    user_turn = client.calls[0][1].content
    assert "Berlin" in user_turn
    assert "data, not instructions" in user_turn


def test_metadata_records_model_dialect_and_prompt_versions(retail_ddl: str) -> None:
    client = ScriptedClient([envelope("valid", "SELECT 1", "One.")])
    result = generate_query(request(retail_ddl), client=client)
    assert result.metadata.model == "scripted/model"
    assert result.metadata.dialect == "sqlite"
    assert result.metadata.validator == "ephemeral_sqlite"
    assert result.metadata.prompt_versions["query.system"] == "v1"
    assert result.metadata.latency_ms >= 0


def test_schema_generation_repairs_invalid_ddl() -> None:
    client = ScriptedClient(
        [
            envelope("valid", "CREATE TABLE book (id INTEGER PRIMARY KEY, id TEXT)", "One table."),
            envelope("valid", "CREATE TABLE book (id INTEGER PRIMARY KEY, title TEXT)", "One."),
        ]
    )
    result = generate_schema(SchemaRequest(description="a library"), client=client)
    assert result.response_class == "valid"
    assert result.metadata.attempts[0].failure_kind == "binder"
    assert len(result.metadata.attempts) == 2


def test_schema_generation_rejects_dml_in_the_ddl() -> None:
    client = ScriptedClient(
        [
            envelope("valid", "CREATE TABLE b (id INT); INSERT INTO b VALUES (1)", "Seeded."),
            envelope("valid", "CREATE TABLE b (id INTEGER PRIMARY KEY)", "One table."),
        ]
    )
    result = generate_schema(SchemaRequest(description="a library"), client=client)
    assert result.metadata.attempts[0].failure_kind == "safety_gate"
    assert result.response_class == "valid"


def test_scripted_client_runs_dry_when_over_asked(retail_ddl: str) -> None:
    client = ScriptedClient([])
    with pytest.raises(AssertionError):
        generate_query(request(retail_ddl), client=client)
