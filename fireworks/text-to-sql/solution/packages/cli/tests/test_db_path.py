"""`t2s db path` exists so a human can check the workbench against the file itself."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest
from click.testing import CliRunner

from t2s_cli.cli import cli


@pytest.fixture
def sample_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    monkeypatch.setenv("T2S_SAMPLE_DB_DIR", str(tmp_path))
    database_id = uuid.uuid4()
    conn = sqlite3.connect(tmp_path / f"{database_id.hex}.sqlite3")
    conn.executescript("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT);")
    conn.execute("INSERT INTO widgets VALUES (1, 'sprocket')")
    conn.commit()
    conn.close()
    return database_id


def test_the_short_id_the_chat_prints_is_enough(sample_db: uuid.UUID) -> None:
    """The chat shows 8 characters, so 8 characters must resolve."""
    result = CliRunner().invoke(cli, ["db", "path", sample_db.hex[:8]])
    assert result.exit_code == 0
    assert result.output.strip().endswith(f"{sample_db.hex}.sqlite3")


def test_a_full_uuid_with_dashes_also_resolves(sample_db: uuid.UUID) -> None:
    result = CliRunner().invoke(cli, ["db", "path", str(sample_db)])
    assert result.exit_code == 0


def test_the_generated_command_is_read_only_and_runs(sample_db: uuid.UUID) -> None:
    """An independent check must never be able to mutate what it is checking."""
    result = CliRunner().invoke(
        cli, ["db", "path", sample_db.hex[:8], "--sql", "SELECT name FROM widgets"]
    )
    assert result.exit_code == 0
    command = result.output.strip()
    assert command.startswith("sqlite3 -readonly ")
    assert "SELECT name FROM widgets" in command


def test_an_unknown_id_fails_clearly(sample_db: uuid.UUID) -> None:
    result = CliRunner().invoke(cli, ["db", "path", "ffffffff"])
    assert result.exit_code != 0
    assert "No sample database" in result.output
