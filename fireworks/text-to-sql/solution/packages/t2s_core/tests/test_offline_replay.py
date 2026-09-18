"""End-to-end generation against real, recorded model output — with no network.

This is the test that proves D8: every fixture here came from a live call to
``accounts/fireworks/models/kimi-k2p7-code``, and the whole pipeline (prompt
rendering → envelope invariants → D9 gate → ephemeral SQLite binder → repair
loop) runs over it offline and deterministically.
"""

from __future__ import annotations

import pytest

from t2s_core.fixtures import recorded_client
from t2s_core.fixtures.scenarios import QUERY_SCENARIOS, REPAIR_DEMOS, SCHEMA_SCENARIOS
from t2s_core.generate import generate_query, generate_schema
from t2s_core.validation import EphemeralSqliteValidator


@pytest.mark.parametrize("scenario", QUERY_SCENARIOS, ids=[s.name for s in QUERY_SCENARIOS])
def test_query_scenarios_replay(scenario: object) -> None:
    result = generate_query(scenario.request, client=recorded_client())  # type: ignore[attr-defined]
    expected = scenario.expect  # type: ignore[attr-defined]
    if expected is not None:
        assert result.response_class == expected, result.prose
    assert result.prose.strip(), "finding #3: prose must never come back empty"
    assert result.metadata.attempts
    if result.response_class == "valid":
        assert result.query
        assert result.metadata.attempts[-1].ok


@pytest.mark.parametrize("scenario", SCHEMA_SCENARIOS, ids=[s.name for s in SCHEMA_SCENARIOS])
def test_schema_scenarios_replay(scenario: object) -> None:
    result = generate_schema(scenario.request, client=recorded_client())  # type: ignore[attr-defined]
    expected = scenario.expect  # type: ignore[attr-defined]
    if expected is not None:
        assert result.response_class == expected, result.prose
    if result.response_class == "valid":
        assert result.query
        assert "CREATE TABLE" in result.query.upper()


def test_generated_schema_supports_a_generated_query() -> None:
    """The two halves compose: DDL from generate_schema is a valid substrate for
    the query validator, which is the eval harness's substrate too (D3)."""
    ddl = generate_schema(SCHEMA_SCENARIOS[0].request, client=recorded_client()).query
    assert ddl is not None
    verdict = EphemeralSqliteValidator().check("SELECT COUNT(*) FROM books", ddl, "sqlite")
    assert verdict.ok, verdict.error


def test_injection_attempts_never_yield_ddl() -> None:
    """Both attacks were refused live; the gate would have caught them anyway."""
    attacks = [s for s in QUERY_SCENARIOS if s.name.startswith("injection")]
    assert len(attacks) == 2
    for scenario in attacks:
        result = generate_query(scenario.request, client=recorded_client())
        assert result.response_class != "error" or result.query is None
        sql = (result.query or "").upper()
        for forbidden in ("DROP", "DELETE", "INSERT", "UPDATE", "ALTER", "ATTACH", "PRAGMA"):
            assert forbidden not in sql


@pytest.mark.parametrize("demo", REPAIR_DEMOS, ids=[d.name for d in REPAIR_DEMOS])
def test_repair_demos_replay_with_both_attempts(demo: object) -> None:
    """The repair loop, end to end, over a real live repair turn.

    Turn 1 is a synthetic hallucination (the live model abstained rather than
    hallucinating, 24 calls running). Turn 2 is a genuine API response to our
    repair prompt, so what is being proven here is that the repair template
    recovers a real model.
    """
    result = generate_query(demo.request, client=recorded_client())  # type: ignore[attr-defined]
    attempts = result.metadata.attempts

    assert len(attempts) == 2
    assert attempts[0].ok is False
    assert attempts[0].failure_kind == "binder"
    assert "no such column" in (attempts[0].failure_message or "")
    assert attempts[1].ok is True
    assert result.metadata.repairs_used == 1
    assert result.response_class == demo.expect  # type: ignore[attr-defined]


def test_the_repairable_demo_ends_in_working_sql() -> None:
    """Attempt 1 invents ``signup_date``; attempt 2 uses the real ``signed_up``."""
    demo = next(d for d in REPAIR_DEMOS if d.name == "repair_demo_wrong_column_name")
    result = generate_query(demo.request, client=recorded_client())

    assert result.response_class == "valid"
    assert result.query is not None
    assert "signup_date" in (result.metadata.attempts[0].candidate_sql or "")
    assert "signed_up" in result.query
    assert "signup_date" not in result.query
    verdict = EphemeralSqliteValidator().check(result.query, demo.request.schema_ddl, "sqlite")
    assert verdict.ok, verdict.error
