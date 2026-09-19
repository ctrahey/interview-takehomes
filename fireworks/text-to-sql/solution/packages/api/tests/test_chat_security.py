"""The chat endpoint is a new channel into the same gates -- and it holds (D9/D12/D15).

Layer 3 already has this evidence for the REPL
(`packages/orchestrator/tests/test_injection_live_fixtures.py`). What is new in
W15 is that an attacker can now reach the router over HTTP, so the assertions
that matter are re-run *through the endpoint*:

* an injection delivered in a chat body is still gated **per directive** -- a
  plan is a bigger blast radius than an intent, and "we checked the first one"
  is not a shortcut this code has;
* catalog access against a sample database is still denied (D12), and the
  refusal is an ordinary 200 error turn that points at the deterministic answer;
* no key material reaches a response body, an activity row, or a log line.

Offline throughout (D8): the injection scripts replay `RecordedClient` fixtures
captured from the live model, so what is asserted is what `kimi-k2p7-code`
actually said when attacked.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlglot
from fastapi.testclient import TestClient
from sqlglot import exp

from t2s_core.ports import InferenceClient
from t2s_nl.clients import offline_client
from t2s_nl.scenarios import SECURITY_SCRIPTS

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

CATALOG_SQL = "SELECT name FROM sqlite_master WHERE type = 'table'"

DDL = (
    "CREATE TABLE customers (id INTEGER NOT NULL PRIMARY KEY, name TEXT NOT NULL);\n"
    "CREATE TABLE invoices (id INTEGER NOT NULL PRIMARY KEY, customer_id INTEGER NOT NULL, "
    "balance_cents INTEGER NOT NULL, FOREIGN KEY (customer_id) REFERENCES customers (id));\n"
)

DATA = {
    "tables": [
        {"name": "customers", "columns": ["id", "name"], "rows": [["1", "Ada"], ["2", "Grace"]]},
        {
            "name": "invoices",
            "columns": ["id", "customer_id", "balance_cents"],
            "rows": [["1", "1", "500"], ["2", "2", "0"]],
        },
    ],
    "notes": "two customers, two invoices",
}


def has_ddl_or_dml(sql: str) -> bool:
    """AST, not string matching -- `SELECT 'DROP TABLE t' AS note` is harmless."""
    for statement in sqlglot.parse(sql, read="sqlite"):
        if statement is None:
            continue
        if any(isinstance(node, _FORBIDDEN) for node in statement.walk()):
            return True
    return False


@pytest.mark.parametrize("script_name", sorted(SECURITY_SCRIPTS))
def test_no_injection_through_the_chat_endpoint_produces_ddl_or_dml(
    script_name: str,
    make_client: Callable[[InferenceClient], TestClient],
    make_session: Callable[[TestClient], str],
    sample_db_dir: Path,
) -> None:
    client = make_client(offline_client())
    session_id = make_session(client)

    turns = []
    for utterance in SECURITY_SCRIPTS[script_name]:
        response = client.post(f"/sessions/{session_id}/chat", json={"utterance": utterance})
        assert response.status_code == 200, response.text
        turns.extend(r["turn"] for r in response.json()["results"])

    # Every directive of every plan, not just the last turn of each utterance.
    attacked = [t for t in turns if t["intent"] in {"query", "execute", "corrective"}]
    assert attacked, f"{script_name}: the script never reached the query path"
    for turn in attacked:
        assert turn["ddl"] is None
        if turn["sql"]:
            assert not has_ddl_or_dml(turn["sql"]), "DDL/DML reached the client as SQL"
        for attempt in turn["attempts"]:
            if attempt["ok"] and attempt["candidate_sql"]:
                assert not has_ddl_or_dml(attempt["candidate_sql"])

    # The schema the attack tried to drop is still there, read deterministically.
    models = client.get("/data-models").json()
    assert models, "the attack destroyed the session's data model"


def test_catalog_access_is_denied_through_chat_and_says_why(
    chat_doubles: SimpleNamespace,
    make_client: Callable[[InferenceClient], TestClient],
    make_session: Callable[[TestClient], str],
    sample_db_dir: Path,
) -> None:
    """D12: reconnaissance against a persisted sample database is refused, not served.

    The refusal arrives as a 200 error turn -- the request succeeded and the
    answer is "no, and here is the endpoint that answers this properly".
    """
    client = make_client(
        chat_doubles.ScriptedChatClient(
            router=[
                chat_doubles.plan(chat_doubles.directive("create_schema", text="a shop")),
                chat_doubles.plan(chat_doubles.directive("load_data", row_count=2)),
                chat_doubles.plan(
                    chat_doubles.directive("query", text="what tables exist?"),
                    chat_doubles.directive("execute", referent="last_query"),
                ),
            ],
            envelope=[
                chat_doubles.envelope("valid", query=DDL, prose="Two tables."),
                chat_doubles.envelope("valid", query=CATALOG_SQL, prose="The catalogue."),
            ],
            data=[DATA],
        )
    )
    session_id = make_session(client)
    for utterance in ("model a shop", "load it with data"):
        assert (
            client.post(f"/sessions/{session_id}/chat", json={"utterance": utterance}).status_code
            == 200
        )

    body = client.post(
        f"/sessions/{session_id}/chat", json={"utterance": "what tables exist? run it"}
    ).json()

    executed = body["results"][-1]["turn"]
    assert executed["intent"] == "execute"
    assert executed["kind"] == "error"
    assert executed["table"] is None, "catalog rows were returned despite the denial"
    assert "denied" in executed["text"] and "D12" in executed["text"]
    # The denial is recorded, not silent: the step's activity failed.
    execute_rows = [a for a in body["activities"] if a["kind"] == "query.execute"]
    assert execute_rows and any(a["status"] == "error" for a in execute_rows)


def test_no_key_material_reaches_a_response_an_activity_row_or_a_log_line(
    chat_doubles: SimpleNamespace,
    make_client: Callable[[InferenceClient], TestClient],
    make_session: Callable[[TestClient], str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D9: the key is environment-only and never echoed, including by this surface.

    The SSE frames are covered by the activity page assertion below: both are
    rendered by `ActivityOut`, deliberately, so that a row cannot serialise one
    way live and another way on replay.
    """
    sentinel = "fw-SENTINEL-do-not-leak-3f9c2a"
    monkeypatch.setenv("FIREWORKS_API_KEY", sentinel)
    caplog.set_level(logging.DEBUG)

    client = make_client(
        chat_doubles.ScriptedChatClient(
            router=[chat_doubles.plan(chat_doubles.directive("create_schema", text="a shop"))],
            envelope=[chat_doubles.envelope("valid", query=DDL, prose="Two tables.")],
        )
    )
    session_id = make_session(client)

    chat = client.post(f"/sessions/{session_id}/chat", json={"utterance": "model a shop"})
    activities = client.get(f"/sessions/{session_id}/activities")
    spec = client.get("/openapi.json")

    for response in (chat, activities, spec):
        assert sentinel not in response.text
    assert sentinel not in json.dumps(activities.json())
    assert sentinel not in caplog.text
