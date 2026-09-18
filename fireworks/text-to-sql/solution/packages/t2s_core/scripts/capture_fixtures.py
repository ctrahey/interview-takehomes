"""Capture live Fireworks responses into replayable fixtures (D8).

    uv run python packages/t2s_core/scripts/capture_fixtures.py [--overwrite] [scenario ...]

The API key is read from ``FIREWORKS_API_KEY`` or, failing that, from
``~/.fireworks-key`` as opaque bytes. It is never printed, and never written into
a fixture: fixtures record the request messages and the response body only.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from pydantic import SecretStr

from t2s_core import FireworksClient, FireworksConfig, RecordingClient
from t2s_core.config import DEFAULT_MODEL
from t2s_core.fixtures import FIXTURE_DIR
from t2s_core.fixtures.scenarios import QUERY_SCENARIOS, SCHEMA_SCENARIOS
from t2s_core.generate import generate_query, generate_schema


def load_key() -> str:
    key = os.environ.get("FIREWORKS_API_KEY", "")
    if key:
        return key
    path = Path.home() / ".fireworks-key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    raise SystemExit("no FIREWORKS_API_KEY and no ~/.fireworks-key")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", help="scenario names (default: all)")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dir", default=str(FIXTURE_DIR))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    config = FireworksConfig(
        api_key=SecretStr(load_key()),
        model=os.environ.get("T2S_MODEL", DEFAULT_MODEL),
    )
    print(f"model: {config.model}")
    live = FireworksClient(config)
    client = RecordingClient(live, args.dir, overwrite=args.overwrite)

    wanted = set(args.names)
    calls = 0
    try:
        for scenario in QUERY_SCENARIOS:
            if wanted and scenario.name not in wanted:
                continue
            result = generate_query(scenario.request, client=client)
            calls += len(result.metadata.attempts)
            _report(scenario.name, scenario.expect, result)
        for scenario in SCHEMA_SCENARIOS:
            if wanted and scenario.name not in wanted:
                continue
            result = generate_schema(scenario.request, client=client)
            calls += len(result.metadata.attempts)
            _report(scenario.name, scenario.expect, result)
    finally:
        live.close()

    print(f"\n{calls} live call(s); {len(client.written)} fixture(s) written to {args.dir}")
    return 0


def _report(name: str, expect: str | None, result: object) -> None:
    got = getattr(result, "response_class", "?")
    metadata = getattr(result, "metadata", None)
    attempts = getattr(metadata, "attempts", [])
    flag = "ok " if (expect is None or expect == got) else "DIFF"
    print(f"{flag} {name}: {got} ({len(attempts)} attempt(s))")
    for attempt in attempts:
        if not attempt.ok:
            print(
                f"     attempt {attempt.index} rejected [{attempt.failure_kind}]: "
                f"{(attempt.failure_message or '')[:120]}"
            )
    query = getattr(result, "query", None)
    if query:
        print(f"     {' '.join(str(query).split())[:160]}")


if __name__ == "__main__":
    sys.exit(main())
