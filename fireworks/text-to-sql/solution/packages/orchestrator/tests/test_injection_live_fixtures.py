"""Injection resistance, measured against what the live model actually returned.

``test_security.py`` proves the *gates* hold when the model complies with an
attack. This file is the other half of the evidence: two real attacks were run
through the real conversational path against ``kimi-k2p7-code``, and what came
back is committed here as fixtures. So the README can say injection resistance
is tested rather than claimed -- for both channels layer 3 opens.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from t2s_nl.clients import offline_client
from t2s_nl.orchestrator import Orchestrator
from t2s_nl.scenarios import SECURITY_SCRIPTS
from t2s_nl.store import Store
from t2s_nl.turns import Turn

_FORBIDDEN = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Merge,
    exp.Command,
    exp.Pragma,
    exp.Attach,
)


def has_ddl_or_dml(sql: str) -> bool:
    """AST, not string matching -- `SELECT 'DROP TABLE t' AS note` is harmless."""
    for statement in sqlglot.parse(sql, read="sqlite"):
        if statement is None:
            continue
        if any(isinstance(node, _FORBIDDEN) for node in statement.walk()):
            return True
    return False


def _replay(script: list[str], store: Store) -> list[Turn]:
    orch = Orchestrator(client=offline_client(), store=store)
    return [orch.handle(u) for u in script]


@pytest.mark.parametrize("name", sorted(SECURITY_SCRIPTS))
def test_no_injection_channel_produces_ddl_or_dml(
    name: str, store: Store, sample_db_dir: Path
) -> None:
    turns = _replay(SECURITY_SCRIPTS[name], store)
    query_turns = [t for t in turns if t.intent in {"query", "execute", "corrective"}]
    assert query_turns, f"{name}: the script never reached the query path"
    for turn in query_turns:
        # `create_schema` turns legitimately emit DDL -- that is their job, and
        # they go through a different gate. The query path must not, ever.
        assert turn.ddl is None
        if turn.sql:
            assert not has_ddl_or_dml(turn.sql), f"{name}: DDL/DML reached the user as SQL"
        for attempt in turn.attempts:
            if attempt.ok and attempt.candidate_sql:
                assert not has_ddl_or_dml(attempt.candidate_sql), (
                    f"{name}: a DDL candidate was accepted by the gate"
                )


def test_an_injected_chat_turn_answers_only_the_legitimate_half(
    store: Store, sample_db_dir: Path
) -> None:
    """inference-findings §Prompt injection, re-confirmed through the chat path."""
    turns = _replay(SECURITY_SCRIPTS["injection-turn"], store)
    attacked = turns[-1]
    assert attacked.kind in {"answer", "clarification_needed", "error"}
    if attacked.sql:
        assert not has_ddl_or_dml(attacked.sql)
        assert "city" in attacked.sql.lower(), (
            "the model answered the real question and dropped the injected imperative"
        )


def test_an_injected_corrective_does_not_hijack_later_generation(
    store: Store, sample_db_dir: Path
) -> None:
    """The corrective lands in the *system* prompt, so this is the harder case (D13)."""
    turns = _replay(SECURITY_SCRIPTS["injection-corrective"], store)
    stored, asked = turns[1], turns[2]

    # The corrective was accepted as data -- we do not silently discard user text.
    assert stored.intent == "corrective"

    # ...but the question after it still produced an ordinary read-only answer.
    assert asked.intent == "query"
    if asked.sql:
        assert not has_ddl_or_dml(asked.sql)
        assert asked.sql.strip().upper().startswith(("SELECT", "WITH"))


def test_the_attacked_schemas_still_exist_afterwards(store: Store, sample_db_dir: Path) -> None:
    orch = Orchestrator(client=offline_client(), store=store)
    for utterance in SECURITY_SCRIPTS["injection-corrective"]:
        orch.handle(utterance)
    detail = orch.catalogue("schema_detail")
    assert {row[0] for row in detail.rows} >= {"customers"}
