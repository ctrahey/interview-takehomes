"""Choosing an inference client, and replaying fixtures from more than one place.

``T2S_OFFLINE=1`` swaps the live Fireworks client for ``RecordedClient`` (D8), so
the chat, the tests and CI all run the same code paths without a network or a
key. Layer 3 needs two fixture directories -- its own captured router and
sample-data exchanges, plus ``t2s_core``'s captured generation exchanges -- so
:class:`ChainedRecordedClient` tries each in turn. The fixture key is a hash of
the whole request (model, messages, schema, budget, temperature), so there is no
ambiguity about which directory answers: at most one of them has the key.

**No key is not a startup failure.** ``t2s_api`` has always deferred building a
live ``FireworksClient`` until a layer-2 request needs one, so the API starts
and serves its whole deterministic layer with no key at all; ``t2s-chat`` used
to exit 2 at startup instead. That asymmetry was recorded in ``memory/status.md``
and is fixed here by :class:`DeferredFireworksClient`, which resolves the key on
first *use*. The consequence is the one that matters for a demo: the workbench
opens, every deterministic read works, and the missing key is explained at the
moment something actually needs inference.

Two conditions in this module mean "there is no model here at all", as opposed
to "the model is having a bad day": ``FixtureNotFound`` (offline replay, nothing
recorded for this request) and :class:`MissingApiKey`. Both are local, permanent
and knowable without touching the network, and **neither can be raised by a
client that has a key and a connection** -- which is what makes them the safe
trigger for ``t2s_nl.offline_router``'s keyword fallback.
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
from t2s_core.errors import FixtureNotFound, T2SError
from t2s_core.ports import InferenceClient, InferenceResponse, Message

__all__ = [
    "CORE_FIXTURE_DIR",
    "NL_FIXTURE_DIR",
    "NO_MODEL_AVAILABLE",
    "ChainedRecordedClient",
    "DeferredFireworksClient",
    "MissingApiKey",
    "api_key_available",
    "is_offline",
    "make_client",
    "offline_client",
    "read_api_key",
]


class MissingApiKey(T2SError):
    """No Fireworks credential, discovered at the point of need rather than at startup.

    A ``T2SError`` so the router's existing degradation path catches it and
    turns it into a conversational turn; never a traceback, and never carrying
    any part of a key (there is none to carry).
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(
            "no Fireworks API key is set, so I cannot call a model"
            + (f" ({detail})" if detail else "")
        )


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


def api_key_available() -> bool:
    """Is there a key to be had, without reading its value?

    Used only to word a banner. Deliberately does not return, log or compare the
    key itself (D9).
    """
    if os.environ.get("FIREWORKS_API_KEY", "").strip():
        return True
    path = Path.home() / ".fireworks-key"
    return path.exists() and bool(path.read_text(encoding="utf-8").strip())


class DeferredFireworksClient:
    """A live client that is not built until something actually calls it.

    This is the whole of the fix for ``t2s-chat``'s fail-fast startup. ``model``
    answers from configuration, so a banner and every deterministic read work
    with no credential present; the key is read on the first :meth:`complete`,
    and its absence surfaces there as :class:`MissingApiKey` -- an expected,
    explainable condition at the point of need, not an exit code before the
    user has typed anything.
    """

    def __init__(self, model: str | None = None) -> None:
        self._model = model or os.environ.get("T2S_MODEL", DEFAULT_MODEL)
        self._inner: InferenceClient | None = None

    @property
    def model(self) -> str:
        return self._model

    def _resolve(self) -> InferenceClient:
        if self._inner is None:
            try:
                key = read_api_key()
            except RuntimeError as exc:
                raise MissingApiKey(str(exc)) from exc
            self._inner = FireworksClient(
                FireworksConfig(api_key=SecretStr(key), model=self._model)
            )
        return self._inner

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
        return self._resolve().complete(
            messages,
            response_schema=response_schema,
            schema_name=schema_name,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        )


#: The two failures that mean "there is no model at all here", as opposed to
#: "the provider is unhappy". Both are local and permanent: a ``RecordedClient``
#: with no matching fixture, and no credential to build a live client with.
#: Deliberately does NOT include ``UpstreamError``/``RateLimited``/
#: ``TransportError`` -- those come from a client that *does* have a key and a
#: network, i.e. from being online, and must never downgrade routing silently.
NO_MODEL_AVAILABLE: tuple[type[T2SError], ...] = (FixtureNotFound, MissingApiKey)


def make_client() -> InferenceClient:
    """The live client, or the fixture replayer when ``T2S_OFFLINE=1``.

    Never raises for a missing key: see :class:`DeferredFireworksClient`.
    """
    if is_offline():
        return offline_client()
    return DeferredFireworksClient()
