"""Runtime configuration. The model ID is config, never a constant (D10)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from pydantic import SecretStr

__all__ = ["DEFAULT_MODEL", "FireworksConfig"]

#: Resolved live and recorded in memory/status.md on 2026-09-18. Overridable via
#: ``T2S_MODEL``; re-verify before trusting it (D10).
DEFAULT_MODEL = "accounts/fireworks/models/kimi-k2p7-code"

#: Query generation. Finding #1: 800 was not enough and truncation is silent
#: until you look at finish_reason.
DEFAULT_MAX_TOKENS = 2000

#: DDL is long. Schema generation gets its own, larger, budget.
DEFAULT_SCHEMA_MAX_TOKENS = 4000


@dataclass(slots=True)
class FireworksConfig:
    """The API key is a ``SecretStr``: it does not appear in ``repr``, in a
    dataclass dump, or in a traceback (D9)."""

    api_key: SecretStr = field(repr=False)
    model: str = DEFAULT_MODEL
    base_url: str = "https://api.fireworks.ai/inference/v1"
    timeout_s: float = 60.0
    connect_timeout_s: float = 10.0
    max_retries: int = 3
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = 0.0
    #: On ``finish_reason == "length"``, retry once with this multiple of the budget.
    truncation_retry_factor: float = 2.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> FireworksConfig:
        source = os.environ if env is None else env
        key = source.get("FIREWORKS_API_KEY", "")
        if not key:
            raise RuntimeError(
                "FIREWORKS_API_KEY is not set. Live calls require it; the offline test "
                "suite does not (it uses RecordedClient)."
            )
        return cls(
            api_key=SecretStr(key),
            model=source.get("T2S_MODEL", DEFAULT_MODEL),
            timeout_s=float(source.get("T2S_TIMEOUT_S", "60")),
            max_retries=int(source.get("T2S_MAX_RETRIES", "3")),
            max_tokens=int(source.get("T2S_MAX_TOKENS", str(DEFAULT_MAX_TOKENS))),
        )
