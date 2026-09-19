"""Layer 3 over HTTP: chat, the activity log, and the live stream (D14/D15).

Three endpoints, one session-scoped conversation:

* ``POST /sessions/{id}/chat`` -- one utterance in, the **executed plan** out.
  Built on ``Orchestrator.plan`` + ``execute_plan`` (what ``run()`` does
  internally), never on ``handle()``, which returns only the last turn of a
  plan and would silently drop the first half of "load sample data and then
  show me a query for unpaid balances".
* ``GET /sessions/{id}/activities`` -- the append-only log, keyset-paginated
  by ``seq``. **Read-only, and there is deliberately no write endpoint**: D14's
  whole claim is that the log is appended by the orchestrator and never
  mutated, and an HTTP surface that could POST a row would be the hole in it.
* ``GET /sessions/{id}/activities/stream`` -- the same rows over SSE as they
  are appended, so a browser can show what the system is doing right now.

`clarification_needed` and `error` turns come back on a **200** carrying their
kind, consistent with layer 2's `response_class` (design §5). So does a halted
plan: the completed prefix is in `results`, the rest is in `not_run`, and
`status` says which happened. Only transport and validation failures are HTTP
errors, and every one of those is RFC 9457 problem+json via `t2s_api.problems`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from foundation.repositories import SessionRepository
from t2s_api.activity_stream import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    ActivityBroker,
    activity_page,
    event_stream,
    resolve_cursor,
)
from t2s_api.chat_schemas import ActivityOut, ActivityPage, ChatRequest, ChatResponse
from t2s_api.deps import DbSession, InjectedInferenceClient
from t2s_api.orchestration import (
    ActivityCollector,
    SessionNotFound,
    build_orchestrator,
    run_chat,
)
from t2s_api.problems import ApiProblem

router = APIRouter(prefix="/sessions", tags=["chat"])

#: SSE needs these three to survive a real deployment: no caching of a stream,
#: no transformation by a proxy, and nginx's buffering explicitly off (without
#: it, nginx holds events until its buffer fills and the "live" display isn't).
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _broker(request: Request) -> ActivityBroker:
    broker: ActivityBroker = request.app.state.activity_broker
    return broker


def _require_session(db: DbSession, session_id: uuid.UUID) -> None:
    if SessionRepository(db).get(session_id) is None:
        raise ApiProblem(404, "not-found", "Session Not Found", f"no session with id {session_id}")


@router.post(
    "/{session_id}/chat",
    response_model=ChatResponse,
    summary="Say one thing to the system, and get the executed plan back",
)
def chat(
    session_id: uuid.UUID,
    body: ChatRequest,
    request: Request,
    client: InjectedInferenceClient,
) -> ChatResponse:
    """Route one utterance into a plan (D15) and execute every directive in it.

    Takes no request-scoped DB session on purpose. The orchestrator opens its
    own short transactions -- each activity row is committed in its own, which
    is what makes a crashed step legible (D14) -- and holding an outer
    transaction open across a multi-second inference call to wrap them would
    defeat exactly that.
    """
    app = request.app
    collector = ActivityCollector()
    try:
        orchestrator = build_orchestrator(
            engine=app.state.engine,
            session_factory=app.state.session_factory,
            session_id=session_id,
            client=client,
            listeners=[collector, _broker(request)],
        )
    except SessionNotFound as exc:
        raise ApiProblem(
            404, "not-found", "Session Not Found", f"no session with id {session_id}"
        ) from exc
    return run_chat(orchestrator, body.utterance, collector=collector)


@router.get(
    "/{session_id}/activities",
    response_model=ActivityPage,
    summary="Read the append-only activity log (D14)",
)
def list_activities(
    session_id: uuid.UUID,
    db: DbSession,
    after_seq: int | None = Query(
        default=None,
        ge=0,
        description="Return rows with seq strictly greater than this. Omit to start at the top.",
    ),
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
) -> ActivityPage:
    """A keyset page over `seq`. There is no write endpoint, by design."""
    _require_session(db, session_id)
    rows = activity_page(db, session_id, after_seq=after_seq, limit=limit)
    activities = [ActivityOut.model_validate(row) for row in rows]
    return ActivityPage(
        session_id=session_id,
        activities=activities,
        after_seq=after_seq,
        limit=limit,
        next_after_seq=activities[-1].seq if activities else None,
        # `len == limit` can mean "exactly caught up", so this is an
        # optimistic hint, not a promise. The cursor is the source of truth:
        # a follow-up page that comes back empty is the real end.
        has_more=len(activities) == limit,
    )


@router.get(
    "/{session_id}/activities/stream",
    summary="Live activity stream (SSE)",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": (
                "A `text/event-stream`. Each row of the activity log arrives as an event "
                "named `activity` whose `data` is an ActivityOut and whose `id` is the "
                "row's `seq`. Lines beginning `:` are heartbeat comments and carry no "
                "payload. An `event: lag` frame means this connection fell too far behind "
                "and rows were dropped -- re-read them from "
                "`GET /sessions/{session_id}/activities?after_seq=`."
            ),
            "content": {
                "text/event-stream": {"schema": {"$ref": "#/components/schemas/ActivityOut"}}
            },
        }
    },
)
async def stream_activities(
    session_id: uuid.UUID,
    request: Request,
    db: DbSession,
    after_seq: int | None = Query(
        default=None,
        ge=0,
        description=(
            "Resume point. Rows with seq > after_seq are replayed before live delivery "
            "begins. Takes precedence over the Last-Event-ID header."
        ),
    ),
    last_event_id: str | None = Header(
        default=None,
        alias="Last-Event-ID",
        description="Sent automatically by a browser EventSource when it reconnects.",
    ),
) -> StreamingResponse:
    """Subscribe to this session's activity log.

    An unknown session is a 404 *before* the stream opens -- a client should
    not have to infer a bad id from a stream that stays silent forever.
    """
    _require_session(db, session_id)
    cursor = resolve_cursor(after_seq, last_event_id)
    stream = event_stream(
        request,
        broker=_broker(request),
        session_factory=request.app.state.session_factory,
        session_id=session_id,
        after_seq=cursor,
    )
    return StreamingResponse(stream, media_type="text/event-stream", headers=SSE_HEADERS)
