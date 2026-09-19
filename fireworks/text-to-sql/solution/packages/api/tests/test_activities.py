"""`GET /sessions/{id}/activities` -- the append-only log over HTTP (D14).

Two things are being asserted, and the second matters more than the first:

* `seq` keyset pagination works and is gapless;
* **the log is read-only over HTTP.** D14's claim is that an activity is a
  record of a transition that happened, and a record that can be written by a
  client is not a record. `foundation.ActivityRepository` has no update or
  delete; this suite makes sure the HTTP surface adds no write path either.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

from fastapi.testclient import TestClient

from t2s_core.ports import InferenceClient

DDL = "CREATE TABLE customers (id INTEGER NOT NULL PRIMARY KEY, name TEXT NOT NULL);\n"


def _busy_session(
    chat_doubles: SimpleNamespace,
    make_client: Callable[[InferenceClient], TestClient],
    make_session: Callable[[TestClient], str],
) -> tuple[TestClient, str]:
    """A session with a handful of activity rows in it."""
    client = make_client(
        chat_doubles.ScriptedChatClient(
            router=[chat_doubles.plan(chat_doubles.directive("create_schema", text="a shop"))],
            envelope=[chat_doubles.envelope("valid", query=DDL, prose="One table.")],
        )
    )
    session_id = make_session(client)
    for _ in range(3):
        response = client.post(f"/sessions/{session_id}/chat", json={"utterance": "model a shop"})
        assert response.status_code == 200, response.text
    return client, session_id


def test_the_whole_log_reads_back_in_seq_order(
    chat_doubles: SimpleNamespace,
    make_client: Callable[[InferenceClient], TestClient],
    make_session: Callable[[TestClient], str],
) -> None:
    client, session_id = _busy_session(chat_doubles, make_client, make_session)

    body = client.get(f"/sessions/{session_id}/activities").json()

    seqs = [a["seq"] for a in body["activities"]]
    assert seqs == sorted(seqs) == list(range(1, len(seqs) + 1))
    assert body["after_seq"] is None
    assert body["next_after_seq"] == seqs[-1]
    assert body["has_more"] is False
    # Every step is a begin/end pair, never one mutable row (D14).
    assert [a["phase"] for a in body["activities"]].count("begin") == len(seqs) // 2
    ends = [a for a in body["activities"] if a["phase"] == "end"]
    assert all(a["duration_ms"] is not None for a in ends)


def test_pagination_by_seq_is_gapless_and_never_overlaps(
    chat_doubles: SimpleNamespace,
    make_client: Callable[[InferenceClient], TestClient],
    make_session: Callable[[TestClient], str],
) -> None:
    client, session_id = _busy_session(chat_doubles, make_client, make_session)
    everything = client.get(f"/sessions/{session_id}/activities").json()["activities"]
    assert len(everything) > 4

    collected: list[dict] = []
    cursor: int | None = None
    for _ in range(20):  # bounded, so a pagination bug fails instead of hanging
        params = {"limit": 3} | ({"after_seq": cursor} if cursor is not None else {})
        page = client.get(f"/sessions/{session_id}/activities", params=params).json()
        assert page["limit"] == 3
        assert len(page["activities"]) <= 3
        if not page["activities"]:
            assert page["has_more"] is False
            assert page["next_after_seq"] is None
            break
        collected.extend(page["activities"])
        cursor = page["next_after_seq"]
    assert [a["seq"] for a in collected] == [a["seq"] for a in everything]


def test_a_cursor_past_the_end_returns_an_empty_page(
    chat_doubles: SimpleNamespace,
    make_client: Callable[[InferenceClient], TestClient],
    make_session: Callable[[TestClient], str],
) -> None:
    client, session_id = _busy_session(chat_doubles, make_client, make_session)
    page = client.get(f"/sessions/{session_id}/activities", params={"after_seq": 100_000}).json()
    assert page["activities"] == []
    assert page["has_more"] is False
    assert page["next_after_seq"] is None


def test_the_log_has_no_write_endpoint(
    chat_doubles: SimpleNamespace,
    make_client: Callable[[InferenceClient], TestClient],
    make_session: Callable[[TestClient], str],
) -> None:
    """Asserted twice: against the live app, and against the published spec."""
    client, session_id = _busy_session(chat_doubles, make_client, make_session)
    path = f"/sessions/{session_id}/activities"
    for call in (
        client.post(path, json={"kind": "forged"}),
        client.put(path, json={"kind": "forged"}),
        client.patch(path, json={"kind": "forged"}),
        client.delete(path),
    ):
        assert call.status_code == 405, call.text

    spec = client.get("/openapi.json").json()
    for documented in spec["paths"]["/sessions/{session_id}/activities"]:
        assert documented == "get", f"the activity log documents a {documented} method"


def test_an_unknown_session_is_problem_json(
    chat_doubles: SimpleNamespace,
    make_client: Callable[[InferenceClient], TestClient],
) -> None:
    client = make_client(chat_doubles.ScriptedChatClient())
    response = client.get("/sessions/1b1a4e9a-0000-4000-8000-000000000000/activities")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_an_over_large_limit_is_rejected_rather_than_served(
    chat_doubles: SimpleNamespace,
    make_client: Callable[[InferenceClient], TestClient],
    make_session: Callable[[TestClient], str],
) -> None:
    client, session_id = _busy_session(chat_doubles, make_client, make_session)
    response = client.get(f"/sessions/{session_id}/activities", params={"limit": 10_000})
    assert response.status_code == 422


def test_every_timestamp_is_utc_marked_however_the_row_was_read(
    chat_doubles: SimpleNamespace,
    make_client: Callable[[InferenceClient], TestClient],
    make_session: Callable[[TestClient], str],
) -> None:
    """The same row must not read as UTC live and as local time on replay.

    `foundation` writes `datetime.now(UTC)`; SQLite has no timezone type and
    returns it naive. Without normalisation, a row published live carried a
    `Z` and the same row replayed out of the database did not -- and a browser
    parses a bare timestamp as local time. Found by driving the live server.
    """
    client, session_id = _busy_session(chat_doubles, make_client, make_session)

    page = client.get(f"/sessions/{session_id}/activities").json()["activities"]
    live = client.post(f"/sessions/{session_id}/chat", json={"utterance": "model a shop"}).json()[
        "activities"
    ]

    assert page and live
    for row in [*page, *live]:
        assert row["at"].endswith("Z"), row["at"]
