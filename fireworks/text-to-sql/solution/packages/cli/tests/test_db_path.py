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


def test_export_produces_a_file_any_tool_can_open(sample_db: uuid.UUID, tmp_path: Path) -> None:
    """The exported file must stand alone -- that is the entire point of it."""
    out = tmp_path / "exported" / "copy.sqlite3"
    result = CliRunner().invoke(cli, ["db", "export", sample_db.hex[:8], "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()

    # Opened with a fresh connection that knows nothing about this project.
    conn = sqlite3.connect(out)
    try:
        assert conn.execute("SELECT name FROM widgets").fetchall() == [("sprocket",)]
    finally:
        conn.close()


def test_export_refuses_to_overwrite(sample_db: uuid.UUID, tmp_path: Path) -> None:
    out = tmp_path / "copy.sqlite3"
    out.write_text("not a database")
    result = CliRunner().invoke(cli, ["db", "export", sample_db.hex[:8], "--out", str(out)])
    assert result.exit_code != 0
    assert "already exists" in result.output
    assert out.read_text() == "not a database"


def test_export_leaves_the_source_untouched(sample_db: uuid.UUID, tmp_path: Path) -> None:
    """VACUUM INTO reads; it must never write back to the sample database."""
    source = Path(CliRunner().invoke(cli, ["db", "path", sample_db.hex[:8]]).output.strip())
    before = source.read_bytes()
    CliRunner().invoke(
        cli, ["db", "export", sample_db.hex[:8], "--out", str(tmp_path / "c.sqlite3")]
    )
    assert source.read_bytes() == before
