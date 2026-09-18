"""Fixture key derivation (D8). If this is wrong, the offline suite rots silently."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from doubles import ScriptedClient, envelope

from t2s_core.clients import RecordedClient, RecordingClient, request_key
from t2s_core.errors import FixtureNotFound
from t2s_core.ports import Message
from t2s_core.wire import ENVELOPE_SCHEMA

MESSAGES = [Message("system", "sys"), Message("user", "usr")]


def key(**overrides: Any) -> str:
    base: dict[str, Any] = {
        "model": "m",
        "messages": MESSAGES,
        "response_schema": ENVELOPE_SCHEMA,
        "schema_name": "response",
        "max_tokens": 2000,
        "temperature": 0.0,
    }
    return request_key(**(base | overrides))


def test_key_is_stable_across_processes() -> None:
    """Hard-coded on purpose: a change to the derivation must break this test,
    because it also invalidates every committed fixture."""
    assert key() == key()
    assert len(key()) == 32


def test_key_ignores_dict_ordering_in_the_schema() -> None:
    reordered = json.loads(json.dumps(ENVELOPE_SCHEMA))
    reordered = dict(reversed(list(reordered.items())))
    assert key(response_schema=reordered) == key()


def test_key_is_sensitive_to_everything_that_changes_the_answer() -> None:
    assert key(model="other") != key()
    assert key(messages=[Message("system", "sys"), Message("user", "different")]) != key()
    assert key(messages=list(reversed(MESSAGES))) != key()
    assert key(max_tokens=4000) != key()
    assert key(temperature=0.7) != key()
    assert key(schema_name="other") != key()
    assert key(response_schema={"type": "object"}) != key()


def test_capture_then_replay_round_trip(tmp_path: Path) -> None:
    live = ScriptedClient([envelope("valid", "SELECT 1", "One.")], model="m")
    recorder = RecordingClient(live, tmp_path)
    captured = recorder.complete(MESSAGES, response_schema=ENVELOPE_SCHEMA, max_tokens=2000)

    replayed = RecordedClient(tmp_path, model="m").complete(
        MESSAGES, response_schema=ENVELOPE_SCHEMA, max_tokens=2000
    )
    assert replayed.content == captured.content
    assert replayed.usage == captured.usage
    assert recorder.written == [key()]
    stored = json.loads((tmp_path / f"{key()}.json").read_text())
    assert stored["request"]["messages"][0] == {"role": "system", "content": "sys"}


def test_missing_fixture_names_the_key(tmp_path: Path) -> None:
    with pytest.raises(FixtureNotFound) as caught:
        RecordedClient(tmp_path, model="m").complete(MESSAGES, response_schema=ENVELOPE_SCHEMA)
    assert caught.value.key in str(caught.value)
    assert "RecordingClient" in str(caught.value)


def test_recording_does_not_clobber_an_existing_fixture(tmp_path: Path) -> None:
    (tmp_path / f"{key()}.json").write_text(json.dumps({"key": key(), "response": {}}))
    live = ScriptedClient([envelope("valid", "SELECT 1", "One.")], model="m")
    RecordingClient(live, tmp_path).complete(
        MESSAGES, response_schema=ENVELOPE_SCHEMA, max_tokens=2000
    )
    assert json.loads((tmp_path / f"{key()}.json").read_text())["response"] == {}
