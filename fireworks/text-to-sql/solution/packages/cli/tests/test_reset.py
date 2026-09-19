"""`t2s reset` is the "throw it all away" escape hatch.

Deliberately a command rather than something the chat can be asked for: every
other destructive action is scoped to one named object and described first,
whereas this one's blast radius is the whole point. A misclassified sentence
must not be able to wipe a workspace.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from t2s_cli.cli import cli


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    databases = tmp_path / "sample_dbs"
    databases.mkdir()
    for index in range(3):
        sqlite3.connect(databases / f"{index:032x}.sqlite3").close()
    store = tmp_path / "meta.sqlite3"
    sqlite3.connect(store).close()
    monkeypatch.setenv("T2S_SAMPLE_DB_DIR", str(databases))
    monkeypatch.setenv("T2S_DB_URL", f"sqlite:///{store}")
    return databases, store


def test_it_removes_every_database_and_the_store(workspace: tuple[Path, Path]) -> None:
    databases, store = workspace
    result = CliRunner().invoke(cli, ["reset", "--yes"])
    assert result.exit_code == 0, result.output
    assert list(databases.glob("*.sqlite3")) == []
    assert not store.exists()


def test_it_refuses_without_confirmation(workspace: tuple[Path, Path]) -> None:
    """The prompt is the safety feature; declining must change nothing."""
    databases, store = workspace
    result = CliRunner().invoke(cli, ["reset"], input="n\n")
    assert result.exit_code != 0
    assert len(list(databases.glob("*.sqlite3"))) == 3
    assert store.exists()


def test_it_says_what_it_will_delete_before_asking(workspace: tuple[Path, Path]) -> None:
    databases, _ = workspace
    result = CliRunner().invoke(cli, ["reset"], input="n\n")
    assert "3 sample database(s)" in result.output
    assert str(databases) in result.output


def test_a_non_file_store_is_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T2S_DB_URL can point at Postgres; deleting that is not ours to guess at."""
    databases = tmp_path / "sample_dbs"
    databases.mkdir()
    sqlite3.connect(databases / f"{0:032x}.sqlite3").close()
    monkeypatch.setenv("T2S_SAMPLE_DB_DIR", str(databases))
    monkeypatch.setenv("T2S_DB_URL", "postgresql://t2s@localhost/t2s")

    result = CliRunner().invoke(cli, ["reset", "--yes"])
    assert result.exit_code == 0
    assert "points elsewhere" in result.output
    assert list(databases.glob("*.sqlite3")) == []


def test_nothing_to_do_is_not_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "sample_dbs"
    empty.mkdir()
    monkeypatch.setenv("T2S_SAMPLE_DB_DIR", str(empty))
    monkeypatch.setenv("T2S_DB_URL", f"sqlite:///{tmp_path / 'absent.sqlite3'}")
    result = CliRunner().invoke(cli, ["reset", "--yes"])
    assert result.exit_code == 0
    assert "Nothing to reset" in result.output
