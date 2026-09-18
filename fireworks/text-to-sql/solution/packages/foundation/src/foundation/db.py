"""Engine / session factory for foundation's own persistence store.

This is the metadata database (projects, sessions, data models, schemas,
queries, ...) -- not to be confused with the *sample* SQLite databases that
`foundation.sample_db` creates and destroys on behalf of users. Two very
different lifecycles share the SQLite engine only incidentally.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from foundation.models import Base

DEFAULT_SQLITE_URL = "sqlite:///:memory:"


def create_foundation_engine(url: str = DEFAULT_SQLITE_URL, *, echo: bool = False) -> Engine:
    """Create the SQLAlchemy engine for foundation's own metadata store."""
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, echo=echo, connect_args=connect_args)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    """Create all tables. Idempotent -- safe to call on every process start."""
    Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker[OrmSession]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[OrmSession]) -> Iterator[OrmSession]:
    """A transactional scope: commits on clean exit, rolls back on exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
