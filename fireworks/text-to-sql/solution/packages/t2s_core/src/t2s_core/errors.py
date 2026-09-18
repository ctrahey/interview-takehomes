"""Typed failure modes for t2s_core.

Two families, and the distinction matters:

* ``InferenceError`` and its subclasses are *transport / protocol* failures. They
  propagate out of :func:`t2s_core.generate_query`; the caller (HTTP layer) maps
  them to a 502-class problem document.
* ``RepairableFailure`` is a *semantic* failure of a candidate answer. It never
  escapes: it is fed straight back to the model as another turn of the repair
  loop. Three distinct causes (bad JSON / broken envelope invariant, safety-gate
  rejection, engine binder error) deliberately share one type so the loop has a
  single code path -- see ``generate.py``.
"""

from __future__ import annotations

__all__ = [
    "FixtureNotFound",
    "InferenceError",
    "InvalidResponse",
    "RateLimited",
    "RepairableFailure",
    "SafetyViolation",
    "T2SError",
    "TransportError",
    "TruncatedResponse",
    "UpstreamError",
]


class T2SError(Exception):
    """Base class for everything this package raises."""


# --------------------------------------------------------------------------
# Inference transport failures
# --------------------------------------------------------------------------
class InferenceError(T2SError):
    """A call to the inference provider failed."""

    def __init__(self, message: str, *, request_id: str | None = None, code: str | None = None):
        super().__init__(message)
        self.request_id = request_id
        self.code = code

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} (request_id={self.request_id})" if self.request_id else base


class TransportError(InferenceError):
    """Network-level failure: connect error, read timeout, DNS, TLS."""


class RateLimited(InferenceError):
    """HTTP 429, after the bounded retry budget was exhausted."""


class UpstreamError(InferenceError):
    """A non-retryable HTTP error (4xx other than 429), or 5xx after retries."""


class TruncatedResponse(InferenceError):
    """``finish_reason == "length"``.

    Finding #1 from the live probe: truncation yields *invalid JSON*, not a schema
    violation, so it must be detected from ``finish_reason`` **before** parsing.
    It is its own typed failure, retried once with a larger token budget, and is
    never surfaced to callers as a parse error.
    """

    def __init__(self, message: str, *, max_tokens: int, request_id: str | None = None):
        super().__init__(message, request_id=request_id, code="truncated")
        self.max_tokens = max_tokens


class InvalidResponse(InferenceError):
    """The provider returned a body we cannot interpret at all (no choices, etc.)."""


class FixtureNotFound(T2SError):
    """RecordedClient was asked to replay a request it has no fixture for."""

    def __init__(self, key: str, fixture_dir: str):
        super().__init__(
            f"no recorded fixture for request key {key!r} in {fixture_dir!r}; "
            "re-capture with t2s_core.clients.RecordingClient"
        )
        self.key = key
        self.fixture_dir = fixture_dir


# --------------------------------------------------------------------------
# Repairable (in-loop) failures
# --------------------------------------------------------------------------
class SafetyViolation(T2SError):
    """The D9 safety gate rejected a statement. Raised by ``validation.safety``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class RepairableFailure(T2SError):
    """A candidate answer the model can plausibly fix if we tell it what broke."""

    def __init__(self, kind: str, message: str, *, candidate_sql: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.candidate_sql = candidate_sql
