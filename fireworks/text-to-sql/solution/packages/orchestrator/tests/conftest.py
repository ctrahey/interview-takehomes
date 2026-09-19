"""Shared fixtures: an isolated store and an isolated sample-database directory.

Every test gets its own temp directory for both, so nothing here can touch
``~/.t2s`` -- the developer's real chat history and sample databases are not
test fixtures.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from t2s_nl.store import Store  # noqa: E402


@pytest.fixture
def sample_db_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "sample_dbs"
    directory.mkdir()
    monkeypatch.setenv("T2S_SAMPLE_DB_DIR", str(directory))
    return directory


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'foundation.sqlite3'}"


@pytest.fixture
def store(db_url: str, sample_db_dir: Path) -> Iterator[Store]:
    yield Store(db_url)
