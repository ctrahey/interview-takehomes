"""`GET /sessions/{id}/activities/stream` -- the live "what is it doing" display.

SSE rather than websockets: the traffic is one-way, it survives proxies, and
`EventSource` is three lines in a browser. What has to be true for it to be
worth having:

* rows arrive **while the run that appends them is still going**, not after it;
* a client that reconnects (`Last-Event-ID`) or resumes (`?after_seq=`) sees
  every row it missed and no row twice;
* a client that walks away leaves no subscription behind;
* an idle connection gets heartbeats so a proxy does not close it;
* it works cross-origin, which is where SSE most often breaks.

**Why these run against a real uvicorn on a loopback port** rather than
`TestClient`: starlette's `TestClient` buffers a response to completion before
returning it, so an endless stream hangs it forever -- and the properties above
are precisely the ones that only exist on a real connection (a real disconnect,
a real proxy-visible header, a real interleaving of a POST with an open GET).
Nothing here leaves the machine and nothing needs a key: the app is still wired
to a scripted `InferenceClient` (D8).
"""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from t2s_api.app import DEFAULT_CORS_ORIGINS
from t2s_core.ports import InferenceClient

DDL = (
    "CREATE TABLE customers (id INTEGER NOT NULL PRIMARY KEY, name TEXT NOT NULL);\n"
    "CREATE TABLE invoices (id INTEGER NOT NULL PRIMARY KEY, customer_id INTEGER NOT NULL, "
    "balance_cents INTEGER NOT NULL, FOREIGN KEY (customer_id) REFERENCES customers (id));\n"
)


@pytest.fixture
def app(chat_doubles: Any, make_app: Callable[[InferenceClient], FastAPI]) -> FastAPI:
    """An app whose model always answers 'here is a two-table schema'."""
    return make_app(
        chat_doubles.ScriptedChatClient(
            router=[chat_doubles.plan(chat_doubles.directive("create_schema", text="a shop"))],
            envelope=[chat_doubles.envelope("valid", query=DDL, prose="Two tables.")],
        )
    )


@pytest.fixture
def server(app: FastAPI) -> Iterator[str]:
    """Serve `app` on a loopback port for the duration of one test."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, log_level="warning", lifespan="off")
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: instance.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not instance.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        instance.should_exit = True
        thread.join(timeout=20)


@pytest.fixture
def http(server: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=server, timeout=httpx.Timeout(30.0)) as client:
        yield client


def session_of(http: httpx.Client) -> str:
    response = http.post("/sessions", json={})
    assert response.status_code == 201, response.text
    session_id: str = response.json()["id"]
    return session_id


def _frames(lines: Iterator[str], *, stop_after: int, deadline: float) -> list[dict[str, Any]]:
    """Parse SSE frames off a line iterator until `stop_after` events are seen."""
    frames: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in lines:
        if time.monotonic() > deadline:  # pragma: no cover - only on a hang
            raise AssertionError(f"timed out after {len(frames)} frames")
        if line.startswith(":"):
            frames.append({"comment": line[1:].strip()})
        elif line.startswith("id:"):
            current["id"] = int(line[3:].strip())
        elif line.startswith("event:"):
            current["event"] = line[6:].strip()
        elif line.startswith("data:"):
            current["data"] = json.loads(line[5:].strip())
        elif line == "" and current:
            frames.append(current)
            current = {}
        if sum(1 for f in frames if "event" in f) >= stop_after:
            break
    return frames


def _events(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for f in frames if f.get("event") == "activity"]


def _drain_preamble(lines: Iterator[str]) -> None:
    for line in lines:
        if line.startswith(":") and "backlog replayed" in line:
            return
    raise AssertionError("the stream closed before the backlog was replayed")


def test_the_stream_delivers_rows_of_a_real_run_as_it_happens(http: httpx.Client) -> None:
    """Connect, then run a turn in another thread: the rows arrive on the wire."""
    session_id = session_of(http)
    posted: list[httpx.Response] = []
    with http.stream("GET", f"/sessions/{session_id}/activities/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache, no-transform"
        assert response.headers["x-accel-buffering"] == "no"

        lines = response.iter_lines()
        # Drain the preamble first, so the subscription is registered before
        # the run starts -- the same ordering a browser gets.
        _drain_preamble(lines)

        def run_turn() -> None:
            with httpx.Client(base_url=str(http.base_url), timeout=30.0) as poster:
                posted.append(
                    poster.post(f"/sessions/{session_id}/chat", json={"utterance": "model a shop"})
                )

        worker = threading.Thread(target=run_turn)
        worker.start()
        try:
            frames = _frames(lines, stop_after=4, deadline=time.monotonic() + 30)
        finally:
            worker.join(timeout=30)

    assert posted and posted[0].status_code == 200, posted[0].text if posted else "no POST"
    events = _events(frames)
    assert len(events) >= 4
    assert [e["id"] for e in events] == sorted(e["id"] for e in events)
    assert [e["data"]["seq"] for e in events] == [e["id"] for e in events]

    first = events[0]["data"]
    assert first["kind"] == "router.classify"
    assert first["phase"] == "begin"
    assert first["status"] == "running"
    assert {e["data"]["kind"] for e in events} >= {"router.classify", "schema.generate"}
    # An `end` row carries the timing that makes this a progress display rather
    # than a ticker.
    ends = [e["data"] for e in events if e["data"]["phase"] == "end"]
    assert ends and all(e["duration_ms"] is not None for e in ends)
    # The same rows are in the POST's own response, with the same seqs: the
    # stream is a view of the log, not a second source of truth.
    assert [a["seq"] for a in posted[0].json()["activities"]][: len(events)] == [
        e["id"] for e in events
    ]


def test_a_reconnect_replays_exactly_what_was_missed(
    http: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Last-Event-ID` and `?after_seq=` are the same cursor, and neither repeats."""
    # A fast heartbeat so the "nothing to replay" case below is observable in a
    # test rather than in fifteen seconds of silence.
    monkeypatch.setenv("T2S_SSE_HEARTBEAT_SECONDS", "0.05")
    session_id = session_of(http)
    assert (
        http.post(f"/sessions/{session_id}/chat", json={"utterance": "model a shop"}).status_code
        == 200
    )
    logged = http.get(f"/sessions/{session_id}/activities").json()["activities"]
    assert len(logged) >= 4
    midpoint = logged[1]["seq"]
    expected = [row["seq"] for row in logged if row["seq"] > midpoint]

    for headers, params in (
        ({"Last-Event-ID": str(midpoint)}, {}),
        ({}, {"after_seq": midpoint}),
    ):
        with http.stream(
            "GET",
            f"/sessions/{session_id}/activities/stream",
            headers=headers,
            params=params,
        ) as response:
            frames = _frames(
                response.iter_lines(), stop_after=len(expected), deadline=time.monotonic() + 30
            )
        assert [e["data"]["seq"] for e in _events(frames)] == expected

    # A cursor at the end replays nothing and waits, rather than repeating.
    with http.stream(
        "GET",
        f"/sessions/{session_id}/activities/stream",
        params={"after_seq": logged[-1]["seq"]},
    ) as response:
        lines = response.iter_lines()
        _drain_preamble(lines)
        tail = [next(lines) for _ in range(6)]
    assert not any(line.startswith(("id:", "event:", "data:")) for line in tail), tail
    assert any("keep-alive" in line for line in tail), tail


def test_an_idle_connection_gets_heartbeat_comments(
    http: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silent SSE socket is closed by proxies; a comment frame is the fix."""
    monkeypatch.setenv("T2S_SSE_HEARTBEAT_SECONDS", "0.05")
    session_id = session_of(http)
    comments: list[str] = []
    with http.stream("GET", f"/sessions/{session_id}/activities/stream") as response:
        deadline = time.monotonic() + 15
        for line in response.iter_lines():
            if line.startswith(":"):
                comments.append(line)
            if sum("keep-alive" in c for c in comments) >= 2:
                break
            assert time.monotonic() < deadline, "no heartbeat arrived"
    assert sum("keep-alive" in c for c in comments) >= 2


def test_a_disconnected_client_leaves_no_subscription_behind(
    app: FastAPI, http: httpx.Client
) -> None:
    """The no-leak story: the generator's `finally` unsubscribes, always."""
    broker = app.state.activity_broker
    session_id = session_of(http)
    assert broker.subscriber_count() == 0

    with http.stream("GET", f"/sessions/{session_id}/activities/stream") as response:
        _drain_preamble(response.iter_lines())
        assert broker.subscriber_count(session_id=uuid.UUID(session_id)) == 1

    # Closing the response disconnects the client. Give the server loop a
    # moment to notice, then assert nothing is left registered.
    deadline = time.monotonic() + 15
    while broker.subscriber_count() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert broker.subscriber_count() == 0

    # And the conversation is unharmed: a dropped viewer costs it nothing.
    assert (
        http.post(f"/sessions/{session_id}/chat", json={"utterance": "model a shop"}).status_code
        == 200
    )


def test_the_stream_is_reachable_cross_origin(http: httpx.Client) -> None:
    """The usual place SSE breaks: the CORS header must be on the stream itself."""
    origin = DEFAULT_CORS_ORIGINS[0]
    session_id = session_of(http)

    preflight = http.request(
        "OPTIONS",
        f"/sessions/{session_id}/activities/stream",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == origin

    with http.stream(
        "GET", f"/sessions/{session_id}/activities/stream", headers={"Origin": origin}
    ) as response:
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        assert response.headers["access-control-allow-credentials"] == "true"

    evil = http.request(
        "OPTIONS",
        f"/sessions/{session_id}/activities/stream",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert evil.headers.get("access-control-allow-origin") is None


def test_an_unknown_session_is_a_404_before_the_stream_opens(http: httpx.Client) -> None:
    """A client should not have to infer a bad id from a stream that stays silent."""
    response = http.get("/sessions/1b1a4e9a-0000-4000-8000-000000000000/activities/stream")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
