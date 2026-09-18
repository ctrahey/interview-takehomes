from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session as OrmSession

from foundation.db import create_foundation_engine, init_db, make_session_factory


@pytest.fixture
def db(tmp_path: Path) -> Iterator[OrmSession]:
    """A fresh, file-backed SQLite metadata store, tables created, per test."""
    engine = create_foundation_engine(f"sqlite:///{tmp_path / 'foundation.sqlite3'}")
    init_db(engine)
    factory = make_session_factory(engine)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def managed_sample_db_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the sample-database managed directory at a per-test temp dir."""
    sample_dir = tmp_path / "sample_dbs"
    monkeypatch.setenv("T2S_SAMPLE_DB_DIR", str(sample_dir))
    return sample_dir
