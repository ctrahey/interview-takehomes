"""Layer 1: sessions (design §5). `project_id` is optional (MAIN.md clarification 2);
omitted, the repository resolves the default project via `foundation.bootstrap`."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from foundation.repositories import SessionRepository
from t2s_api.deps import DbSession
from t2s_api.problems import ApiProblem
from t2s_api.schemas import SessionAttachDataModel, SessionCreate, SessionOut

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=SessionOut, status_code=201)
def create_session(body: SessionCreate, db: DbSession) -> SessionOut:
    session_row = SessionRepository(db).create(
        project_id=body.project_id, slug=body.slug, summary=body.summary
    )
    return SessionOut.model_validate(session_row)


@router.get("/{session_id}", response_model=SessionOut)
def get_session(session_id: uuid.UUID, db: DbSession) -> SessionOut:
    session_row = SessionRepository(db).get(session_id)
    if session_row is None:
        raise ApiProblem(404, "not-found", "Session Not Found", f"no session with id {session_id}")
    return SessionOut.model_validate(session_row)


@router.post("/{session_id}/data-models", response_model=SessionOut)
def attach_data_model(
    session_id: uuid.UUID, body: SessionAttachDataModel, db: DbSession
) -> SessionOut:
    """A session MAY refer to any number of data models (design §4, MAIN.md)."""
    SessionRepository(db).attach_data_model(session_id, body.data_model_id)
    session_row = SessionRepository(db).get(session_id)
    assert session_row is not None  # attach_data_model would have raised otherwise
    return SessionOut.model_validate(session_row)
