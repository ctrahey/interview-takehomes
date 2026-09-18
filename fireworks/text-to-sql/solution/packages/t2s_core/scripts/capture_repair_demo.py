"""Capture a live repair turn (D4 / D8).

Turn 1 is synthetic: a hallucinated ``email`` column, written straight into a
fixture and flagged ``"synthetic": true``. Turn 2 is a real call to Fireworks
carrying our repair template, the rejected SQL, and the binder's exact words --
which is the part actually worth evidencing, because it tests whether the repair
prompt recovers a real model rather than whether a mock returns what we told it
to.

    uv run python packages/t2s_core/scripts/capture_repair_demo.py
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from capture_fixtures import load_key
from pydantic import SecretStr

from t2s_core import FireworksClient, FireworksConfig, RecordingClient
from t2s_core.clients.recorded import request_key
from t2s_core.config import DEFAULT_MAX_TOKENS, DEFAULT_MODEL
from t2s_core.fixtures import FIXTURE_DIR
from t2s_core.fixtures.scenarios import REPAIR_DEMOS
from t2s_core.generate import generate_query
from t2s_core.models import Usage
from t2s_core.ports import InferenceResponse, Message


class SeededClient:
    """Returns a canned first turn, then delegates every later turn to the live
    client -- so the repair prompt is answered by the real model."""

    def __init__(self, first: str, inner: Any) -> None:
        self._first = first
        self._inner = inner
        self.calls = 0

    @property
    def model(self) -> str:
        return str(self._inner.model)

    def complete(
        self,
        messages: Sequence[Message],
        *,
        response_schema: Mapping[str, Any],
        schema_name: str = "response",
        max_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> InferenceResponse:
        self.calls += 1
        if self.calls == 1:
            _write_synthetic_fixture(
                model=self.model,
                messages=messages,
                response_schema=response_schema,
                schema_name=schema_name,
                max_tokens=max_tokens,
                temperature=temperature,
                content=self._first,
            )
            return InferenceResponse(
                content=self._first,
                model=self.model,
                finish_reason="stop",
                usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            )
        return self._inner.complete(
            messages,
            response_schema=response_schema,
            schema_name=schema_name,
            max_tokens=max_tokens,
            temperature=temperature,
        )


def _write_synthetic_fixture(
    *,
    model: str,
    messages: Sequence[Message],
    response_schema: Mapping[str, Any],
    schema_name: str,
    max_tokens: int | None,
    temperature: float,
    content: str,
) -> None:
    key = request_key(
        model=model,
        messages=messages,
        response_schema=response_schema,
        schema_name=schema_name,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    payload = {
        "key": key,
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "synthetic": True,
        "why_synthetic": (
            "Hand-written first turn for the repair demonstration: a hallucinated "
            "customers.email column. The live model abstained correctly instead of "
            "hallucinating, so the failure being repaired is manufactured; the repair "
            "turn that follows is a real API response."
        ),
        "request": {
            "model": model,
            "messages": [m.as_wire() for m in messages],
            "schema_name": schema_name,
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        "response": {
            "content": content,
            "model": model,
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "latency_ms": 0,
            "request_id": None,
        },
    }
    path = FIXTURE_DIR / f"{key}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote synthetic turn-1 fixture {key}")


def main() -> int:
    wanted = set(sys.argv[1:])
    config = FireworksConfig(api_key=SecretStr(load_key()), model=DEFAULT_MODEL)
    live = FireworksClient(config)
    recorder = RecordingClient(live, FIXTURE_DIR, overwrite=True)
    live_calls = 0
    try:
        for demo in REPAIR_DEMOS:
            if wanted and demo.name not in wanted:
                continue
            client = SeededClient(demo.synthetic_turn, recorder)
            result = generate_query(demo.request, client=client)
            live_calls += client.calls - 1
            print(f"\n{demo.name}: {result.response_class} (expected {demo.expect})")
            for attempt in result.metadata.attempts:
                print(
                    f"  attempt {attempt.index}: ok={attempt.ok} kind={attempt.failure_kind} "
                    f"sql={' '.join((attempt.candidate_sql or '').split())[:90]!r}"
                )
                if attempt.failure_message:
                    print(f"      -> {attempt.failure_message}")
            print(f"  prose: {result.prose}")
    finally:
        live.close()
    print(f"\n{live_calls} live call(s); budget {DEFAULT_MAX_TOKENS} tokens")
    return 0


if __name__ == "__main__":
    sys.exit(main())
