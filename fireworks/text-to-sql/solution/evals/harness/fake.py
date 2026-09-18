"""A scripted :class:`~t2s_core.ports.InferenceClient` for exercising the harness
without a network.

This exists so the runner, the scorer and the report writer can be tested end to
end offline, and so ``--fake`` can prove the plumbing before a live run spends
tokens. It is an *oracle*, not a model: it looks the question up in the corpus
and replies with the gold SQL, deliberately corrupting a deterministic subset so
the report has both outcomes in it.

Any report produced from this client is stamped ``live: false`` and carries a
banner saying the numbers are not evidence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from evals.harness.corpus import Corpus

from t2s_core.models import Usage
from t2s_core.ports import InferenceResponse, Message

__all__ = ["FakeOracleClient"]

_FAKE_PROMPT_TOKENS = 900
_FAKE_COMPLETION_TOKENS = 120


def _bucket(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16) % 100


@dataclass(slots=True)
class FakeOracleClient:
    """``corrupt_pct`` of items get a deliberately broken candidate on the first
    turn; on a repair turn the oracle "fixes" it, so a loop-on arm recovers them
    and a loop-off arm does not."""

    corpus: Corpus
    corrupt_pct: int = 0
    model_id: str = "fake/oracle"
    calls: int = field(default=0)

    @property
    def model(self) -> str:
        return self.model_id

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
        prompt = "\n".join(m.content for m in messages if m.role == "user")
        # Longest match first, so one question being a prefix of another cannot
        # resolve to the wrong item.
        item = next(
            (
                i
                for i in sorted(self.corpus.items, key=lambda i: -len(i.question))
                if i.question in prompt
            ),
            None,
        )
        is_repair = any(m.role == "assistant" for m in messages)

        if item is None or item.gold_sql is None:
            payload = {
                "response_class": "clarification_needed",
                "query": None,
                "prose": "Which metric did you have in mind?",
                "error": None,
            }
        elif _bucket(item.id) < self.corrupt_pct and not is_repair:
            payload = {
                "response_class": "valid",
                "query": "SELECT no_such_column FROM sqlite_master",
                "prose": "Here you go.",
                "error": None,
            }
        else:
            payload = {
                "response_class": "valid",
                "query": item.gold_sql,
                "prose": "Here you go.",
                "error": None,
            }

        return InferenceResponse(
            content=json.dumps(payload),
            model=self.model_id,
            finish_reason="stop",
            usage=Usage(
                prompt_tokens=_FAKE_PROMPT_TOKENS,
                completion_tokens=_FAKE_COMPLETION_TOKENS,
                total_tokens=_FAKE_PROMPT_TOKENS + _FAKE_COMPLETION_TOKENS,
            ),
            latency_ms=5,
        )
