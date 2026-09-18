"""The API's own metadata-store engine wiring.

Separate from `foundation.db` because the API's default (no `T2S_API_DB_URL`
override) needs pooling behaviour `foundation.db.create_foundation_engine`
does not configure: FastAPI runs synchronous path functions in a thread pool,
and SQLAlchemy's default pool for a bare ``sqlite:///:memory:`` URL
(`SingletonThreadPool`) hands each *thread* its own private in-memory
database -- verified empirically, and a real footgun for an in-process demo
server. `StaticPool` (one shared connection) is what actually keeps state
visible across requests.

Set `T2S_API_DB_URL` for a persistent, file-backed store (e.g. across
restarts); the unset default stays in-memory and touches no disk, which is
also exactly what the offline test suite wants.
"""

from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import StaticPool

from foundation.db import create_foundation_engine

__all__ = ["DB_URL_ENV", "default_engine"]

DB_URL_ENV = "T2S_API_DB_URL"


def default_engine() -> Engine:
    """The engine `create_app` uses when no explicit `engine=` is supplied."""
    url = os.environ.get(DB_URL_ENV)
    if url:
        return create_foundation_engine(url)

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine
