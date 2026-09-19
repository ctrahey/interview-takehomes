"""The live activity stream: SSE over the D14 append-only log.

Chris's ask is the TUI's "what is it doing right now" in a browser, and the
constraint that comes with it is the one `t2s_nl.live.LiveActivityDisplay`
already honours: **the display must not be able to slow or break the work.**

How that is achieved here, end to end:

* :class:`ActivityBroker` is an `ActivityListener`. The orchestrator calls
  `on_begin`/`on_end` on the worker thread running the request. All the broker
  does there is `loop.call_soon_threadsafe(put_nowait)` per subscriber -- no
  lock held across I/O, no queue that can block, no join, no await. If a
  subscriber's queue is full the record is **dropped and counted**, and the
  stream tells that client to re-read the log from `after_seq`; a slow browser
  cannot apply backpressure to an inference call. `ActivityEmitter` swallows
  and logs listener exceptions on top of that (D14), so even a bug here costs
  nobody their query.

* The streaming endpoint is `async`, the chat endpoint is a plain `def` and
  therefore runs in FastAPI's threadpool. Neither blocks the event loop, which
  is what lets the stream deliver a row *while* the plan that appended it is
  still running.

* After the initial backlog read, the generator never touches the database
  again. A long-lived connection costs one asyncio queue and nothing else.

Reconnect story (deliberately two mechanisms, one shape):

* Every event carries ``id: <seq>``. A browser `EventSource` therefore resends
  ``Last-Event-ID`` automatically on reconnect and gets the rows it missed.
* A client that is not a browser -- or one that has state of its own -- passes
  ``?after_seq=`` instead. Both resolve to the same cursor; the header wins
  only when the query parameter is absent.
* On connect the backlog is read from the database **after** the subscription
  is registered, and live rows at or below the last replayed `seq` are
  skipped. That ordering is what makes the join gapless and duplicate-free.

A dropped client is detected two ways: the heartbeat comment fails to write, or
`request.is_disconnected()` reports it between records. Either way the
generator's `finally` unsubscribes, so there is no listener and no thread left
behind.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from sqlalchemy import select

from foundation.models import Activity
from t2s_api.chat_schemas import ActivityOut
from t2s_nl.activity import ActivityRecord

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session as OrmSession
    from sqlalchemy.orm import sessionmaker
    from starlette.requests import Request

__all__ = [
    "DEFAULT_HEARTBEAT_SECONDS",
    "MAX_PAGE_LIMIT",
    "ActivityBroker",
    "Subscription",
    "activity_page",
    "event_stream",
    "heartbeat_seconds",
    "resolve_cursor",
]

logger = logging.getLogger("t2s_api.activity_stream")

#: Comment frames keep an idle connection alive through proxies that time out a
#: silent socket (nginx's default is 60s; Heroku's is 55s).
DEFAULT_HEARTBEAT_SECONDS = 15.0
HEARTBEAT_ENV = "T2S_SSE_HEARTBEAT_SECONDS"

#: How many rows one subscriber may fall behind before we stop buffering for
#: it. A whole money-path conversation is ~40 rows, so a client that is 1000
#: behind is not reading, and buffering for it indefinitely is a memory leak
#: with extra steps.
SUBSCRIBER_QUEUE_SIZE = 1000

MAX_PAGE_LIMIT = 500
DEFAULT_PAGE_LIMIT = 100

#: Replayed on connect before switching to live rows. Bounded for the same
#: reason the page limit is: a client that wants the whole history of a long
#: session should page the REST endpoint, which is built for it.
BACKLOG_LIMIT = 500


def heartbeat_seconds() -> float:
    raw = os.environ.get(HEARTBEAT_ENV)
    if not raw:
        return DEFAULT_HEARTBEAT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_HEARTBEAT_SECONDS
    return value if value > 0 else DEFAULT_HEARTBEAT_SECONDS


class Subscription:
    """One connected client's queue, plus the loop it must be fed from."""

    def __init__(self, session_id: uuid.UUID, loop: asyncio.AbstractEventLoop) -> None:
        self.session_id = session_id
        self.queue: asyncio.Queue[ActivityRecord] = asyncio.Queue(SUBSCRIBER_QUEUE_SIZE)
        self.dropped = 0
        self._loop = loop

    def offer(self, record: ActivityRecord) -> None:
        """Hand a record over from a worker thread. Never blocks, never raises."""
        try:
            self._loop.call_soon_threadsafe(self._put, record)
        except RuntimeError:
            # The loop is closed (shutdown, or a disconnect racing this call).
            # The subscription is already on its way out; dropping is correct.
            self.dropped += 1

    def _put(self, record: ActivityRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            self.dropped += 1


class ActivityBroker:
    """Fans activity rows out to the SSE clients watching each session.

    An `ActivityListener` (D14): the chat endpoint passes it to the
    `Orchestrator`, which calls it for every row it appends. Records carry
    their own `session_id`, so routing needs no extra bookkeeping.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscriptions: dict[uuid.UUID, set[Subscription]] = {}

    # -- subscription ------------------------------------------------------
    def subscribe(self, session_id: uuid.UUID) -> Subscription:
        """Register the *calling* coroutine's loop as this client's delivery loop."""
        subscription = Subscription(session_id, asyncio.get_running_loop())
        with self._lock:
            self._subscriptions.setdefault(session_id, set()).add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        with self._lock:
            watchers = self._subscriptions.get(subscription.session_id)
            if watchers is None:
                return
            watchers.discard(subscription)
            if not watchers:
                del self._subscriptions[subscription.session_id]

    def subscriber_count(self, session_id: uuid.UUID | None = None) -> int:
        with self._lock:
            if session_id is not None:
                return len(self._subscriptions.get(session_id, ()))
            return sum(len(s) for s in self._subscriptions.values())

    # -- ActivityListener --------------------------------------------------
    def on_begin(self, record: ActivityRecord) -> None:
        self.publish(record)

    def on_end(self, record: ActivityRecord) -> None:
        self.publish(record)

    def publish(self, record: ActivityRecord) -> None:
        """Called on the orchestrator's thread. Copy the set, then release the lock."""
        with self._lock:
            watchers = list(self._subscriptions.get(record.session_id, ()))
        for subscription in watchers:
            try:
                subscription.offer(record)
            except Exception as exc:  # noqa: BLE001 - a viewer must not kill a turn
                logger.warning("dropping activity for a subscriber: %s", exc)


# ---------------------------------------------------------------------------
# Reading the log
# ---------------------------------------------------------------------------
def activity_page(
    db: OrmSession,
    session_id: uuid.UUID,
    *,
    after_seq: int | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
) -> list[Activity]:
    """Rows with ``seq > after_seq``, oldest first.

    A plain read. `ActivityRepository` has `append` and readers and no update
    or delete path by design (D14); this adds a keyset page over the same
    table rather than a new capability, and nothing in this package can write
    to it.
    """
    stmt = select(Activity).where(Activity.session_id == session_id)
    if after_seq is not None:
        stmt = stmt.where(Activity.seq > after_seq)
    return list(db.execute(stmt.order_by(Activity.seq).limit(limit)).scalars())


def resolve_cursor(after_seq: int | None, last_event_id: str | None) -> int | None:
    """`?after_seq=` wins; else `Last-Event-ID`, which browsers resend for us."""
    if after_seq is not None:
        return after_seq
    if last_event_id:
        try:
            return int(last_event_id.strip())
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# SSE framing
# ---------------------------------------------------------------------------
def format_event(record: ActivityOut) -> str:
    payload = record.model_dump(mode="json")
    return f"id: {record.seq}\nevent: activity\ndata: {json.dumps(payload)}\n\n"


def format_comment(text: str) -> str:
    return f": {text}\n\n"


async def event_stream(
    request: Request,
    *,
    broker: ActivityBroker,
    session_factory: sessionmaker[OrmSession],
    session_id: uuid.UUID,
    after_seq: int | None,
    heartbeat: float | None = None,
) -> AsyncIterator[str]:
    """The SSE body: backlog, then live rows, with heartbeats in the gaps."""
    interval = heartbeat if heartbeat is not None else heartbeat_seconds()
    subscription = broker.subscribe(session_id)
    cursor = after_seq
    try:
        # `retry` is a hint to EventSource for how long to wait before
        # reconnecting; the reconnect itself carries Last-Event-ID.
        yield "retry: 3000\n\n"
        yield format_comment(f"stream open for session {session_id}")

        backlog = await asyncio.to_thread(_read_backlog, session_factory, session_id, after_seq)
        for row in backlog:
            cursor = row.seq
            yield format_event(row)
        yield format_comment("backlog replayed")

        while True:
            if await request.is_disconnected():
                break
            try:
                record = await asyncio.wait_for(subscription.queue.get(), timeout=interval)
            except TimeoutError:
                # Nothing happened for `interval` seconds. The comment both
                # keeps proxies from closing the socket and surfaces a dead
                # client, because writing to one raises.
                yield format_comment("keep-alive")
                continue
            if subscription.dropped:
                missed, subscription.dropped = subscription.dropped, 0
                yield (
                    "event: lag\ndata: "
                    + json.dumps({"dropped": missed, "resume_after_seq": cursor})
                    + "\n\n"
                )
            if cursor is not None and record.seq <= cursor:
                continue  # already replayed from the backlog; join is gapless
            cursor = record.seq
            yield format_event(ActivityOut.from_record(record))
    finally:
        # Reached on clean break, on client disconnect (the generator is
        # closed/cancelled), and on error. This is the whole no-leak story.
        broker.unsubscribe(subscription)


def _read_backlog(
    session_factory: sessionmaker[OrmSession],
    session_id: uuid.UUID,
    after_seq: int | None,
) -> list[ActivityOut]:
    """Read the pre-connection rows on a worker thread, not the event loop."""
    with session_factory() as db:
        rows = activity_page(db, session_id, after_seq=after_seq, limit=BACKLOG_LIMIT)
        return [ActivityOut.model_validate(row) for row in rows]
