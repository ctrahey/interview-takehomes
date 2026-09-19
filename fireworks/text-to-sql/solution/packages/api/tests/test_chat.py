"""`POST /sessions/{id}/chat` -- one utterance in, the executed plan out (D15).

The properties under test are the ones a web UI cannot work without:

* a two-directive plan comes back as **two turns, in order**, not one flattened
  answer (which is what `Orchestrator.handle()` would have given us);
* a refused plan says nothing ran;
* a halted plan says what *did* run, and what did not, on a 200 -- a halt is a
  conversational outcome, not an HTTP failure;
* `clarification_needed` and `error` are 200s carrying their kind, consistent
  with layer 2's `response_class` (design §5).

Every test is offline (D8): the app is wired with a scripted `InferenceClient`,
so nothing here touches a network or a key.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from t2s_core.ports import InferenceClient

DDL = (
    "CREATE TABLE customers (id INTEGER NOT NULL PRIMARY KEY, name TEXT NOT NULL);\n"
    "CREATE TABLE invoices (id INTEGER NOT NULL PRIMARY KEY, customer_id INTEGER NOT NULL, "
    "balance_cents INTEGER NOT NULL, FOREIGN KEY (customer_id) REFERENCES customers (id));\n"
)

UNPAID = (
    "SELECT c.name, i.balance_cents FROM customers c "
    "JOIN invoices i ON i.customer_id = c.id WHERE i.balance_cents > 0"
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


def _say(client: TestClient, session_id: str, utterance: str) -> dict:
    response = client.post(f"/sessions/{session_id}/chat", json={"utterance": utterance})
    assert response.status_code == 200, response.text
    body: dict = response.json()
    return body


# ---------------------------------------------------------------------------
# One directive
# ---------------------------------------------------------------------------
def test_a_single_directive_turn_returns_the_plan_and_its_one_turn(
    chat_doubles: SimpleNamespace,
    make_client: Callable[[InferenceClient], TestClient],
    make_session: Callable[[TestClient], str],
) -> None:
    client = make_client(
        chat_doubles.ScriptedChatClient(
            router=[chat_doubles.plan(chat_doubles.directive("create_schema", text="a shop"))],
            envelope=[chat_doubles.envelope("valid", query=DDL, prose="Two tables.")],
        )
    )
    session_id = make_session(client)

    body = _say(client, session_id, "model a shop with customers and invoices")

    assert body["status"] == "completed"
    assert body["stopped_reason"] is None
    assert body["not_run"] == []
    assert [d["intent"] for d in body["plan"]["directives"]] == ["create_schema"]

    (result,) = body["results"]
    assert result["position"] == 1
    assert result["directive"]["intent"] == "create_schema"
    assert result["turn"]["kind"] == "answer"
    assert "CREATE TABLE" in result["turn"]["ddl"]
    assert result["turn"]["plan_length"] == 1

    # The activities the turn appended are named, and the rows themselves ride
    # along so a UI can show timings without a second round trip (D14).
    assert result["activity_seqs"], "the directive's activity ids are missing"
    assert body["plan"]["activity_seqs"], "the router.classify ids are missing"
    seqs = [a["seq"] for a in body["activities"]]
    assert seqs == sorted(seqs)
    assert set(body["plan"]["activity_seqs"]) | set(result["activity_seqs"]) == set(seqs)
    kinds = {a["kind"] for a in body["activities"]}
    assert {"router.classify", "schema.generate"} <= kinds


def test_clarification_needed_is_a_200_carrying_that_kind(
    chat_doubles: SimpleNamespace,
    make_client: Callable[[InferenceClient], TestClient],
    make_session: Callable[[TestClient], str],
) -> None:
    """Consistent with layer 2: the request succeeded, the answer is a question."""
    client = make_client(
        chat_doubles.ScriptedChatClient(
            router=[
                chat_doubles.plan(
                    chat_doubles.directive("unknown"),
                    confidence="low",
                    clarifying_question="Did you mean the orders table?",
                )
            ]
        )
    )
    session_id = make_session(client)

    body = _say(client, session_id, "purple monkey dishwasher")

    assert body["status"] == "completed"
    (result,) = body["results"]
    assert result["turn"]["kind"] == "clarification_needed"
    assert result["turn"]["text"] == "Did you mean the orders table?"


# ---------------------------------------------------------------------------
# Several directives
# ---------------------------------------------------------------------------
def test_a_two_directive_plan_returns_both_turns_in_order(
    chat_doubles: SimpleNamespace,
    make_client: Callable[[InferenceClient], TestClient],
    make_session: Callable[[TestClient], str],
    sample_db_dir: Path,
) -> None:
    """Chris's *"show me the query for X and sample results"*, over HTTP."""
    client = make_client(
        chat_doubles.ScriptedChatClient(
            router=[
                chat_doubles.plan(chat_doubles.directive("create_schema", text="a shop")),
                chat_doubles.plan(chat_doubles.directive("load_data", row_count=2)),
                chat_doubles.plan(
                    chat_doubles.directive("query", text="who has an unpaid balance?"),
                    chat_doubles.directive("execute", referent="last_query"),
                ),
            ],
            envelope=[
                chat_doubles.envelope("valid", query=DDL, prose="Two tables."),
                chat_doubles.envelope("valid", query=UNPAID, prose="Unpaid balances."),
            ],
            data=[DATA],
        )
    )
    session_id = make_session(client)
    _say(client, session_id, "model a shop")
    _say(client, session_id, "load it with data")

    body = _say(client, session_id, "show me the query for unpaid balances and sample results")

    assert body["status"] == "completed"
    assert [d["intent"] for d in body["plan"]["directives"]] == ["query", "execute"]
    assert [r["position"] for r in body["results"]] == [1, 2]
    assert [r["directive"]["intent"] for r in body["results"]] == ["query", "execute"]
    assert [r["turn"]["plan_position"] for r in body["results"]] == [1, 2]
    assert all(r["turn"]["plan_length"] == 2 for r in body["results"])

    generated, executed = body["results"]
    assert "balance_cents" in generated["turn"]["sql"]
    assert executed["turn"]["table"]["rows"] == [["Ada", "500"]]
    assert executed["turn"]["deterministic_answer"] is True

    # Each directive's activities are attributed to it, and the two sets are
    # disjoint -- that is what makes a per-directive progress display possible.
    first, second = set(generated["activity_seqs"]), set(executed["activity_seqs"])
    assert first and second and not (first & second)
    assert {a["kind"] for a in body["activities"]} >= {"query.generate", "query.execute"}


def test_a_refused_plan_runs_nothing_and_says_so(
    chat_doubles: SimpleNamespace,
    make_client: Callable[[InferenceClient], TestClient],
    make_session: Callable[[TestClient], str],
) -> None:
    """Over the cap: refused whole, never truncated to the first N (D15)."""
    client = make_client(
        chat_doubles.ScriptedChatClient(
            router=[
                chat_doubles.plan(
                    *(chat_doubles.directive("query", text=f"question {i}") for i in range(5))
                )
            ]
        )
    )
    session_id = make_session(client)

    body = _say(client, session_id, "do five things at once")

    assert body["status"] == "refused"
    assert body["plan"]["directives"] == []
    assert body["plan"]["refusal"] and "Nothing was done" in body["plan"]["refusal"]
    assert body["stopped_reason"] == body["plan"]["refusal"]
    (result,) = body["results"]
    assert result["directive"] is None
    assert result["turn"]["kind"] == "error"

    # The refusal itself is evidence: a `router.classify` end row, status
    # "refused", and no generation activity of any kind.
    kinds = {a["kind"] for a in body["activities"]}
    assert kinds == {"router.classify"}
    assert any(a["status"] == "refused" for a in body["activities"])


def test_a_halted_plan_exposes_the_completed_prefix(
    chat_doubles: SimpleNamespace,
    make_client: Callable[[InferenceClient], TestClient],
    make_session: Callable[[TestClient], str],
) -> None:
    """A halt is not an error: the client renders what ran, then why it stopped.

    Three directives, no sample database loaded. The query is written, the
    execute cannot run, and the third directive never happens.
    """
    client = make_client(
        chat_doubles.ScriptedChatClient(
            router=[
                chat_doubles.plan(chat_doubles.directive("create_schema", text="a shop")),
                chat_doubles.plan(
                    chat_doubles.directive("query", text="who has an unpaid balance?"),
                    chat_doubles.directive("execute", referent="last_query"),
                    chat_doubles.directive("query", text="and the totals?"),
                ),
            ],
            envelope=[
                chat_doubles.envelope("valid", query=DDL, prose="Two tables."),
                chat_doubles.envelope("valid", query=UNPAID, prose="Unpaid balances."),
            ],
        )
    )
    session_id = make_session(client)
    _say(client, session_id, "model a shop")

    body = _say(client, session_id, "write that query, run it, and then total it up")

    assert body["status"] == "halted"
    assert len(body["plan"]["directives"]) == 3
    assert [r["directive"]["intent"] for r in body["results"]] == ["query", "execute"]
    assert body["results"][0]["turn"]["kind"] == "answer"
    assert body["results"][0]["turn"]["sql"]
    assert body["results"][1]["turn"]["kind"] == "clarification_needed"
    assert [d["intent"] for d in body["not_run"]] == ["query"]
    assert "stopped after directive 2 of 3" in body["stopped_reason"]
    assert any("not run" in note for note in body["results"][1]["turn"]["notes"])


def test_a_halted_plan_is_not_flattened_into_an_http_error(
    chat_doubles: SimpleNamespace,
    make_client: Callable[[InferenceClient], TestClient],
    make_session: Callable[[TestClient], str],
) -> None:
    """The first directive fails outright; the response is still a 200 plan."""
    client = make_client(
        chat_doubles.ScriptedChatClient(
            router=[
                chat_doubles.plan(chat_doubles.directive("create_schema", text="a shop")),
                chat_doubles.plan(
                    chat_doubles.directive("query", text="what cannot be answered"),
                    chat_doubles.directive("execute", referent="last_query"),
                ),
            ],
            envelope=[
                chat_doubles.envelope("valid", query=DDL, prose="Two tables."),
                chat_doubles.envelope(
                    "error",
                    prose="That is not answerable from this schema.",
                    error={"code": "unanswerable", "message": "no such column"},
                ),
            ],
        )
    )
    session_id = make_session(client)
    _say(client, session_id, "model a shop")

    response = client.post(
        f"/sessions/{session_id}/chat",
        json={"utterance": "answer the impossible and then run it"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "halted"
    assert body["results"][0]["turn"]["kind"] == "error"
    assert [d["intent"] for d in body["not_run"]] == ["execute"]


# ---------------------------------------------------------------------------
# Transport and validation failures are the only HTTP errors
# ---------------------------------------------------------------------------
def test_an_unknown_session_is_problem_json(
    chat_doubles: SimpleNamespace,
    make_client: Callable[[InferenceClient], TestClient],
) -> None:
    client = make_client(chat_doubles.ScriptedChatClient())
    response = client.post(
        "/sessions/1b1a4e9a-0000-4000-8000-000000000000/chat",
        json={"utterance": "hello"},
    )
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["title"] == "Session Not Found"


def test_an_empty_utterance_is_a_422_problem(
    chat_doubles: SimpleNamespace,
    make_client: Callable[[InferenceClient], TestClient],
    make_session: Callable[[TestClient], str],
) -> None:
    client = make_client(chat_doubles.ScriptedChatClient())
    session_id = make_session(client)
    response = client.post(f"/sessions/{session_id}/chat", json={"utterance": ""})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
