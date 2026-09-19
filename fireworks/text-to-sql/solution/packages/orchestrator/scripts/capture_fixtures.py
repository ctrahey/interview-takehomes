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
  corrective, the W18 model-deletion lifecycle (Chris's own three utterances),
  and the prompt-injection attempts. These record every call the path makes,
  including ``t2s_core``'s generation and repair turns.

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
from t2s_nl.router import route
from t2s_nl.scenarios import (
    CONTEXTS,
    LIFECYCLE_SCRIPT,
    MONEY_PATH,
    ROUTER_CASES,
    ROUTER_SCOPE_CASES,
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


def capture_router(client: CachingRecorder) -> int:
    """Replay/record every router case and report how many disagree.

    The DIFF count is the D16 check after a schema change: if the plan-shaped
    response makes ``reasoning_effort="none"`` misclassify, it shows up here as
    a number, at capture time, rather than as a mystery in CI.
    """
    print(f"router: {len(ROUTER_CASES)} utterances x {len(CONTEXTS)} contexts")
    diffs = 0
    scopes: dict[tuple[str, str], str] = {}
    for utterance, context_name, expected in ROUTER_CASES:
        # Capture EVERY utterance against EVERY canonical context, not just the
        # one the case declares. The router context is part of the prompt, so an
        # empty session and a loaded one produce different request keys for the
        # same words -- and offline mode starts every conversation empty.
        # Capturing only the declared context is why T2S_OFFLINE=1 died on "show
        # me my databases" the moment a real user typed it into a fresh session.
        #
        # Iterating CONTEXTS rather than naming them is the W17 fix to the same
        # bug in its next form: a third context was added and a hand-written
        # dict of two would have orphaned every case that used it, silently.
        plans = {
            name: route(utterance, client=client, context=context)
            for name, context in CONTEXTS.items()
        }
        # The expectation belongs to the declared context only: "run that"
        # means something different with nothing to run.
        plan = plans[context_name if context_name in plans else "empty"]
        got = plan.intents
        diffs += got != expected
        shown = " -> ".join(
            d.intent
            + (f"/{d.parameters.inspect_target}" if d.intent == "inspect" else "")
            + (f"[{d.referent}]" if d.referent != "none" else "")
            for d in plan.directives
        )
        flag = "ok " if got == expected else "DIFF"
        print(
            f"  {flag} {utterance[:52]:<54} -> {shown or '(none)'} (wanted {' -> '.join(expected)})"
        )
        scopes[utterance, context_name] = (
            plan.directives[0].parameters.delete_scope if (plan.directives) else "unspecified"
        )
    print(f"  router: {len(ROUTER_CASES) - diffs}/{len(ROUTER_CASES)} matched")

    # W18. The scope is a parameter rather than an intent, so it would be
    # invisible in the table above -- and it is the whole of what W18 added to
    # the router. Reported here so a prompt regression on model-versus-database
    # shows up at capture time, which is where every other routing regression in
    # this project has been caught.
    print(f"\nrouter scope: {len(ROUTER_SCOPE_CASES)} destroy utterances")
    for utterance, context_name, expected_scope in ROUTER_SCOPE_CASES:
        got_scope = scopes.get((utterance, context_name), "(not routed)")
        flag = "ok " if got_scope == expected_scope else "DIFF"
        diffs += got_scope != expected_scope
        print(f"  {flag} {utterance[:52]:<54} -> {got_scope} (wanted {expected_scope})")
    return diffs


def capture_scenario(client: CachingRecorder, name: str, utterances: list[str]) -> None:
    print(f"\nscenario {name}: {len(utterances)} turns")
    workdir = Path(tempfile.mkdtemp(prefix=f"t2s-capture-{name}-"))
    os.environ["T2S_SAMPLE_DB_DIR"] = str(workdir / "dbs")
    try:
        store = Store(f"sqlite:///{workdir / 'foundation.sqlite3'}")
        orch = Orchestrator(client=client, store=store)
        for utterance in utterances:
            for turn in orch.run(utterance):
                summary = (turn.text or "").split("\n")[0][:72]
                position = (
                    f" [{turn.plan_position}/{turn.plan_length}]" if turn.plan_length > 1 else ""
                )
                print(f"  [{turn.kind}/{turn.intent}]{position} {utterance[:44]:<46} {summary}")
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

    diffs = capture_router(client)
    capture_scenario(client, "money-path", MONEY_PATH)
    capture_scenario(client, "lifecycle-model-deletion", LIFECYCLE_SCRIPT)
    for name, utterances in SECURITY_SCRIPTS.items():
        capture_scenario(client, name, utterances)

    print(
        f"\nwrote {len(client.written)} new fixture(s), replayed {client.replayed} "
        f"existing, in {fixture_dir}"
    )
    if diffs:
        print(f"WARNING: {diffs} router case(s) disagreed with the expected plan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
