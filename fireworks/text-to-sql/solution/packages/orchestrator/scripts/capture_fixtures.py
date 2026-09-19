#!/usr/bin/env python
"""Capture live Fireworks exchanges into replayable fixtures (D8).

Run this, with a key, when a prompt template changes or a new scenario is added.
The offline test suite then replays what a real model actually said -- which is
the only kind of fixture worth having, because a hand-authored one asserts our
beliefs about the model rather than its behaviour.

    FIREWORKS_API_KEY=... uv run python packages/orchestrator/scripts/capture_fixtures.py

Two groups are captured:

* **router** -- one call per utterance, against a small number of *canonical*
  contexts, so the intent table in the tests is reproducible without replaying a
  whole conversation.
* **scenarios** -- whole conversations driven through ``Orchestrator.handle``:
  the money path (describe → DDL → database → data → question → SQL → run), a
  corrective, and two prompt-injection attempts. These record every call the
  path makes, including ``t2s_core``'s generation and repair turns.

The key is read as opaque data and held in a ``SecretStr``. It is never printed.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from t2s_core.clients import FireworksClient, RecordedClient, RecordingClient
from t2s_core.config import FireworksConfig
from t2s_core.errors import FixtureNotFound
from t2s_core.ports import InferenceResponse, Message
from t2s_nl.clients import NL_FIXTURE_DIR, read_api_key
from t2s_nl.orchestrator import Orchestrator
from t2s_nl.router import RouterContext, route
from t2s_nl.scenarios import (
    LOADED_CONTEXT,
    MONEY_PATH,
    ROUTER_CASES,
    SECURITY_SCRIPTS,
)
from t2s_nl.store import Store


class CachingRecorder:
    """Replay if a fixture exists, otherwise call live and record it.

    This wrapper is load-bearing, not an optimisation. Two scenarios that open
    with the same utterance share a fixture key, and a plain ``RecordingClient``
    calls the live model for the second one while *keeping* the first one's
    stored response (it does not overwrite). The two then diverge -- the capture
    run continues with DDL the fixture does not contain, so every downstream
    request in that scenario is keyed on text no fixture will ever match, and
    the replay dies with FixtureNotFound.

    Replaying first makes the capture run see exactly what the replay will see,
    so a recorded conversation is consistent by construction. It also makes a
    re-capture cheap: only genuinely new calls reach the API.
    """

    def __init__(self, fixture_dir: Path) -> None:
        config = FireworksConfig(api_key=SecretStr(read_api_key()))
        self._recorder = RecordingClient(FireworksClient(config), fixture_dir)
        self._replayer = RecordedClient(fixture_dir)
        self.replayed = 0

    @property
    def model(self) -> str:
        return self._recorder.model

    @property
    def written(self) -> list[str]:
        return self._recorder.written

    def complete(self, messages: Sequence[Message], **kwargs: Any) -> InferenceResponse:
        try:
            response = self._replayer.complete(messages, **kwargs)
        except FixtureNotFound:
            return self._recorder.complete(messages, **kwargs)
        self.replayed += 1
        return response


def _live_client(fixture_dir: Path) -> CachingRecorder:
    return CachingRecorder(fixture_dir)


def capture_router(client: CachingRecorder) -> None:
    print(f"router: {len(ROUTER_CASES)} utterances")
    for utterance, context_name, expected in ROUTER_CASES:
        context = LOADED_CONTEXT if context_name == "loaded" else RouterContext()
        decision = route(utterance, client=client, context=context)
        flag = "ok " if decision.intent == expected else "DIFF"
        target = decision.parameters.inspect_target
        print(
            f"  {flag} {utterance[:52]:<54} -> {decision.intent}"
            f"{'/' + target if decision.intent == 'inspect' else ''}"
            f" (wanted {expected})"
        )


def capture_scenario(client: CachingRecorder, name: str, utterances: list[str]) -> None:
    print(f"\nscenario {name}: {len(utterances)} turns")
    workdir = Path(tempfile.mkdtemp(prefix=f"t2s-capture-{name}-"))
    os.environ["T2S_SAMPLE_DB_DIR"] = str(workdir / "dbs")
    try:
        store = Store(f"sqlite:///{workdir / 'foundation.sqlite3'}")
        orch = Orchestrator(client=client, store=store)
        for utterance in utterances:
            turn = orch.handle(utterance)
            summary = (turn.text or "").split("\n")[0][:72]
            print(f"  [{turn.kind}/{turn.intent}] {utterance[:44]:<46} {summary}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        os.environ.pop("T2S_SAMPLE_DB_DIR", None)


def main() -> int:
    fixture_dir = NL_FIXTURE_DIR
    fixture_dir.mkdir(parents=True, exist_ok=True)
    try:
        client = _live_client(fixture_dir)
    except RuntimeError as exc:
        print(f"capture: {exc}", file=sys.stderr)
        return 2

    capture_router(client)
    capture_scenario(client, "money-path", MONEY_PATH)
    for name, utterances in SECURITY_SCRIPTS.items():
        capture_scenario(client, name, utterances)

    print(
        f"\nwrote {len(client.written)} new fixture(s), replayed {client.replayed} "
        f"existing, in {fixture_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
