"""Emitting the append-only activity log (D14).

D14's ownership split, restated because it is the only thing keeping this file
honest: **`foundation` persists, the orchestrator emits, surfaces subscribe.**
Nothing here writes a row itself; it calls `ActivityRepository.append`, which
has no update path, and hands the same record to whatever listeners are
attached so a front end can draw a live line.

Two properties are requirements rather than taste:

* **The `begin` row is committed before the work starts.** It is written in its
  own transaction, so a step that dies -- an exception, a `SIGKILL`, a closed
  laptop -- leaves a `begin` with no `end` on disk. That is the evidence D14
  asks for, and it is unobtainable from a mutable "running" row.
* **Listeners cannot slow or break the work.** Every callback is wrapped: an
  exception in a renderer is logged and swallowed, because a broken progress
  line must never cost the user their query. The cost the log itself adds is
  two small INSERTs per step, on the order of a tenth of a millisecond against
  a 7-second inference call.

Usage::

    with emitter.step("data.generate", summary="generating sample data") as step:
        result = generate_rows(...)
        step.record_inference(model=..., tokens=..., request_id=...)
        step.detail = {"tables": 4}

The context manager writes `begin` on entry and `end` on exit, with the
status derived from whether the body raised.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from foundation.models import Activity
from foundation.repositories import ActivityRepository
from t2s_nl.store import Store

__all__ = [
    "ACTIVITY_KINDS",
    "ActivityEmitter",
    "ActivityListener",
    "ActivityRecord",
    "Step",
    "record_of",
]

logger = logging.getLogger("t2s_nl.activity")

#: Every kind this package emits. D14 names all eight; the tuple is asserted
#: against the instrumentation in the test suite so a new step cannot be added
#: without the taxonomy growing to match.
ACTIVITY_KINDS: tuple[str, ...] = (
    "router.classify",
    "schema.generate",
    "data.generate",
    "data.load",
    "query.generate",
    "query.repair",
    "query.execute",
    "corrective.record",
)

#: Human wording for a live line, keyed by kind. Front ends may override.
KIND_LABELS: Mapping[str, str] = {
    "router.classify": "working out what you meant",
    "schema.generate": "designing the schema",
    "data.generate": "generating sample data",
    "data.load": "loading sample data",
    "query.generate": "writing the SQL",
    "query.repair": "repairing the SQL",
    "query.execute": "running the query",
    "corrective.record": "recording that",
}


@dataclass(frozen=True, slots=True)
class ActivityRecord:
    """A detached, immutable view of one persisted row.

    Detached on purpose: listeners run outside the transaction that wrote the
    row, and handing them a live ORM object would give a progress renderer a
    database session it has no business holding.
    """

    seq: int
    session_id: uuid.UUID
    kind: str
    phase: str
    status: str
    at: datetime
    duration_ms: int | None = None
    summary: str | None = None
    detail: Mapping[str, Any] | None = None
    model: str | None = None
    tokens: int | None = None
    request_id: str | None = None

    @property
    def label(self) -> str:
        return self.summary or KIND_LABELS.get(self.kind, self.kind)


def record_of(row: Activity) -> ActivityRecord:
    return ActivityRecord(
        seq=row.seq,
        session_id=row.session_id,
        kind=row.kind,
        phase=row.phase,
        status=row.status,
        at=row.at,
        duration_ms=row.duration_ms,
        summary=row.summary,
        detail=dict(row.detail) if row.detail else None,
        model=row.model,
        tokens=row.tokens,
        request_id=row.request_id,
    )


class ActivityListener(Protocol):
    """A surface that wants to know as steps start and finish.

    ``t2s-chat``'s live line is one of these. So, later, is an SSE endpoint or a
    Slack "typing" indicator -- which is why the orchestrator publishes records
    rather than printing anything itself.
    """

    def on_begin(self, record: ActivityRecord) -> None: ...

    def on_end(self, record: ActivityRecord) -> None: ...


@dataclass(slots=True)
class Step:
    """The handle yielded by :meth:`ActivityEmitter.step`.

    The body of a step fills in what it learned -- how many rows, which model,
    how many tokens -- and the ``end`` row carries it. Nothing set here is
    written until the step closes, so a mid-step mutation is not a mutation of
    anything persisted.
    """

    kind: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)
    status: str = ActivityRepository.OK
    model: str | None = None
    tokens: int | None = None
    request_id: str | None = None
    begin_seq: int = 0

    def record_inference(
        self,
        *,
        model: str | None = None,
        tokens: int | None = None,
        request_id: str | None = None,
    ) -> None:
        """Attach the provenance of an inference call to this step.

        This is what turns "the turn took 9 seconds" into "7.0s of it was one
        `data.generate` call to kimi-k2p7-code, request 01J...". D14: the eval
        harness gets the per-phase breakdown for free.
        """
        if model is not None:
            self.model = model
        if tokens is not None:
            self.tokens = tokens
        if request_id is not None:
            self.request_id = request_id

    def note(self, **detail: Any) -> None:
        self.detail.update(detail)

    def refuse(self, reason: str) -> None:
        """Close this step as a refusal rather than an error or a success."""
        self.status = ActivityRepository.REFUSED
        self.summary = reason


class ActivityEmitter:
    """Appends activities for one session and publishes them to listeners."""

    def __init__(
        self,
        store: Store,
        session_id: uuid.UUID,
        listeners: Sequence[ActivityListener] = (),
    ) -> None:
        self.store = store
        self.session_id = session_id
        self._listeners: list[ActivityListener] = list(listeners)

    # -- subscription -----------------------------------------------------
    def subscribe(self, listener: ActivityListener) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: ActivityListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    # -- emission ---------------------------------------------------------
    @contextmanager
    def step(
        self,
        kind: str,
        *,
        summary: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> Iterator[Step]:
        """Bracket a unit of work with a `begin` row and an `end` row.

        An exception inside the body closes the step with ``status="error"`` and
        the exception's message, then re-raises: the log records what happened
        and the caller's error handling is untouched.
        """
        handle = Step(kind=kind, summary=summary or KIND_LABELS.get(kind, kind))
        if detail:
            handle.detail.update(detail)
        handle.begin_seq = self._append(
            kind=kind,
            phase=ActivityRepository.BEGIN,
            status=ActivityRepository.RUNNING,
            summary=handle.summary,
            detail=dict(handle.detail) or None,
        )
        started = time.perf_counter()
        try:
            yield handle
        except BaseException as exc:  # noqa: BLE001 - re-raised immediately below
            self._append(
                kind=kind,
                phase=ActivityRepository.END,
                status=ActivityRepository.ERROR,
                summary=f"{type(exc).__name__}: {exc}"[:500],
                detail=dict(handle.detail) or None,
                duration_ms=_elapsed_ms(started),
                model=handle.model,
                tokens=handle.tokens,
                request_id=handle.request_id,
            )
            raise
        self._append(
            kind=kind,
            phase=ActivityRepository.END,
            status=handle.status,
            summary=handle.summary,
            detail=dict(handle.detail) or None,
            duration_ms=_elapsed_ms(started),
            model=handle.model,
            tokens=handle.tokens,
            request_id=handle.request_id,
        )

    def note(
        self,
        kind: str,
        *,
        summary: str,
        status: str = ActivityRepository.OK,
        detail: Mapping[str, Any] | None = None,
        duration_ms: int | None = None,
        model: str | None = None,
        tokens: int | None = None,
        request_id: str | None = None,
        at: datetime | None = None,
    ) -> None:
        """Record a step that already happened, as a begin/end pair.

        Used for `query.repair`: the repair loop lives inside `t2s_core` behind
        the `QueryValidator` port (D4), so we learn about each attempt only from
        the result metadata afterwards. Reconstructing the pair from the
        recorded per-attempt latency is honest -- the durations are measured,
        not invented -- and it keeps one shape in the log rather than a third
        phase that means "atomic".
        """
        end = at or datetime.now(UTC)
        self._append(
            kind=kind,
            phase=ActivityRepository.BEGIN,
            status=ActivityRepository.RUNNING,
            summary=summary,
            detail=dict(detail) if detail else None,
            at=end,
        )
        self._append(
            kind=kind,
            phase=ActivityRepository.END,
            status=status,
            summary=summary,
            detail=dict(detail) if detail else None,
            duration_ms=duration_ms,
            model=model,
            tokens=tokens,
            request_id=request_id,
            at=end,
        )

    # -- reads ------------------------------------------------------------
    def history(self, *, limit: int | None = None) -> list[ActivityRecord]:
        with self.store.scope() as db:
            rows = ActivityRepository(db).list_for_session(self.session_id, limit=limit)
            return [record_of(r) for r in rows]

    def latest(self, kind: str, *, status: str | None = "ok") -> ActivityRecord | None:
        """The last completed activity of one kind -- D15's referent resolution."""
        with self.store.scope() as db:
            row = ActivityRepository(db).latest(self.session_id, kind=kind, status=status)
            return record_of(row) if row is not None else None

    def unfinished(self) -> list[ActivityRecord]:
        with self.store.scope() as db:
            return [record_of(r) for r in ActivityRepository(db).unfinished(self.session_id)]

    # -- internals --------------------------------------------------------
    def _append(self, **kwargs: Any) -> int:
        """Write one row in its OWN transaction, then publish it.

        Its own transaction is the point: if the `begin` shared a transaction
        with the work, a crash would roll it back and the log would contain no
        trace of the step that died -- which is the one case the log exists for.
        """
        try:
            with self.store.scope() as db:
                row = ActivityRepository(db).append(self.session_id, **kwargs)
                record = record_of(row)
        except Exception as exc:  # noqa: BLE001 - observability must not break work
            logger.warning("could not append activity %s: %s", kwargs.get("kind"), exc)
            return 0
        self._publish(record)
        return record.seq

    def _publish(self, record: ActivityRecord) -> None:
        callback = "on_begin" if record.phase == ActivityRepository.BEGIN else "on_end"
        for listener in self._listeners:
            try:
                getattr(listener, callback)(record)
            except Exception as exc:  # noqa: BLE001 - a renderer must not kill a turn
                logger.warning("activity listener %r raised: %s", listener, exc)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
