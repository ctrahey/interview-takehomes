"""Choosing an inference client, and replaying fixtures from more than one place.

``T2S_OFFLINE=1`` swaps the live Fireworks client for ``RecordedClient`` (D8), so
the chat, the tests and CI all run the same code paths without a network or a
key. Layer 3 needs two fixture directories -- its own captured router and
sample-data exchanges, plus ``t2s_core``'s captured generation exchanges -- so
:class:`ChainedRecordedClient` tries each in turn. The fixture key is a hash of
the whole request (model, messages, schema, budget, temperature), so there is no
ambiguity about which directory answers: at most one of them has the key.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import SecretStr

import t2s_core
from t2s_core.clients import FireworksClient, RecordedClient
from t2s_core.config import DEFAULT_MODEL, FireworksConfig
from t2s_core.errors import FixtureNotFound
from t2s_core.ports import InferenceClient, InferenceResponse, Message

__all__ = [
    "CORE_FIXTURE_DIR",
    "NL_FIXTURE_DIR",
    "ChainedRecordedClient",
    "is_offline",
    "make_client",
    "offline_client",
    "read_api_key",
]

NL_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "inference"


#: ``t2s_core``'s own captured exchanges, replayed alongside ours.
CORE_FIXTURE_DIR = Path(t2s_core.__file__).parent / "fixtures" / "inference"

_OFFLINE_ENV = "T2S_OFFLINE"


def is_offline(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return source.get(_OFFLINE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


class ChainedRecordedClient:
    """A ``RecordedClient`` over several fixture directories, first hit wins."""

    def __init__(self, *directories: Path | str, model: str | None = None) -> None:
        self._clients = [RecordedClient(d, model=model) for d in directories]
        if not self._clients:
            raise ValueError("ChainedRecordedClient needs at least one fixture directory")

    @property
    def model(self) -> str:
        return self._clients[0].model

    def keys(self) -> list[str]:
        found: set[str] = set()
        for client in self._clients:
            found.update(client.keys())
        return sorted(found)

    def complete(
        self,
        messages: Sequence[Message],
        *,
        response_schema: Mapping[str, Any],
        schema_name: str = "response",
        max_tokens: int | None = None,
        temperature: float = 0.0,
        reasoning_effort: str | None = None,
    ) -> InferenceResponse:
        last: FixtureNotFound | None = None
        for client in self._clients:
            try:
                return client.complete(
                    messages,
                    response_schema=response_schema,
                    schema_name=schema_name,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    reasoning_effort=reasoning_effort,
                )
            except FixtureNotFound as exc:
                last = exc
        assert last is not None
        raise last


def offline_client(*extra_dirs: Path | str) -> ChainedRecordedClient:
    return ChainedRecordedClient(NL_FIXTURE_DIR, CORE_FIXTURE_DIR, *extra_dirs)


def read_api_key() -> str:
    """``FIREWORKS_API_KEY``, else ``~/.fireworks-key`` read as opaque data.

    Read, never sourced (status.md's W0 incident: the file holds a bare token,
    not shell syntax). The value goes straight into a ``SecretStr`` so it cannot
    surface in a repr or a traceback.
    """
    env = os.environ.get("FIREWORKS_API_KEY")
    if env:
        return env.strip()
    path = Path.home() / ".fireworks-key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    raise RuntimeError(
        "No Fireworks API key. Set FIREWORKS_API_KEY or create ~/.fireworks-key, "
        "or run with T2S_OFFLINE=1 to replay recorded fixtures."
    )


def make_client() -> InferenceClient:
    """The live client, or the fixture replayer when ``T2S_OFFLINE=1``."""
    if is_offline():
        return offline_client()
    config = FireworksConfig(
        api_key=SecretStr(read_api_key()),
        model=os.environ.get("T2S_MODEL", DEFAULT_MODEL),
    )
    return FireworksClient(config)
