"""Where durable state lives, and how a turn gets a transaction.

``foundation`` owns 100% of persistence (MAIN.md §Foundation), so this module is
a thin opener: it resolves a database URL, creates the schema if needed, and
hands out transactional scopes. It holds no state of its own beyond the two ids
that identify the conversation, and those are themselves rows in foundation.

The default location is ``~/.t2s/foundation.sqlite3``. That default is the
difference between a demo and the thing that was asked for: close the chat,
reopen it, and the same session -- with its current model, schema, database and
correctives -- is still there.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from foundation.bootstrap import (
    get_or_create_default_project,
    get_or_create_default_session,
)
from foundation.db import create_foundation_engine, init_db, make_session_factory, session_scope
from foundation.models import Session as SessionModel

__all__ = ["DEFAULT_DB_ENV", "Store", "default_database_url"]

DEFAULT_DB_ENV = "T2S_DB_URL"
_DEFAULT_DIR = Path.home() / ".t2s"


def default_database_url() -> str:
    """``T2S_DB_URL`` if set, else a file under ``~/.t2s``.

    A file, not ``:memory:``: an orchestrator whose state dies with the process
    cannot resume a session, and resuming is the point.
    """
    configured = os.environ.get(DEFAULT_DB_ENV)
    if configured:
        return configured
    _DEFAULT_DIR.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{_DEFAULT_DIR / 'foundation.sqlite3'}"


class Store:
    """Owns the engine and resolves the conversation's project/session ids."""

    def __init__(self, url: str | None = None, *, session_slug: str | None = None) -> None:
        self.url = url or default_database_url()
        self.engine = create_foundation_engine(self.url)
        init_db(self.engine)
        self._factory = make_session_factory(self.engine)
        self.project_id, self.session_id = self._resolve(session_slug)

    def _resolve(self, session_slug: str | None) -> tuple[uuid.UUID, uuid.UUID]:
        with self.scope() as db:
            project = get_or_create_default_project(db)
            if session_slug is None:
                session_row = get_or_create_default_session(db, project)
            else:
                session_row = self._named_session(db, project.id, session_slug)
            return project.id, session_row.id

    @staticmethod
    def _named_session(db: OrmSession, project_id: uuid.UUID, slug: str) -> SessionModel:
        existing = db.execute(
            select(SessionModel).where(
                SessionModel.project_id == project_id, SessionModel.slug == slug
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = SessionModel(project_id=project_id, slug=slug)
        db.add(row)
        db.flush()
        return row

    @contextmanager
    def scope(self) -> Iterator[OrmSession]:
        """A transaction: commits on clean exit, rolls back on exception."""
        with session_scope(self._factory) as db:
            yield db
