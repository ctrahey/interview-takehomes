"""``t2s db create|load|query|list|destroy`` -- via foundation.sample_db.

Fully offline: these never touch t2s_core or the network, only a sample
SQLite file under an isolated ``T2S_SAMPLE_DB_DIR`` (see ``sample_db_env`` in
conftest.py).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from click.testing import CliRunner

from t2s_cli.cli import cli

DDL = """CREATE TABLE widgets (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);"""


def _create(runner: CliRunner, tmp_path: Path) -> str:
    schema_path = tmp_path / "schema.sql"
    schema_path.write_text(DDL, encoding="utf-8")
    result = runner.invoke(cli, ["db", "create", "--schema", str(schema_path), "--json"])
    assert result.exit_code == 0, result.output
    database_id: str = json.loads(result.output)["database_id"]
    return database_id


def test_db_create_load_query_list_destroy(
    runner: CliRunner, tmp_path: Path, sample_db_env: Path
) -> None:
    database_id = _create(runner, tmp_path)

    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(
        json.dumps({"widgets": [{"id": 1, "name": "sprocket"}, {"id": 2, "name": "cog"}]}),
        encoding="utf-8",
    )
    loaded = runner.invoke(cli, ["db", "load", database_id, str(dataset_path), "--json"])
    assert loaded.exit_code == 0, loaded.output
    assert json.loads(loaded.output)["rows_inserted"] == 2

    listed = runner.invoke(cli, ["db", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    ids = [entry["database_id"] for entry in json.loads(listed.output)]
    assert database_id in ids

    queried = runner.invoke(
        cli, ["db", "query", database_id, "SELECT name FROM widgets ORDER BY id", "--json"]
    )
    assert queried.exit_code == 0, queried.output
    payload = json.loads(queried.output)
    assert payload["rows"] == [["sprocket"], ["cog"]]
    assert payload["columns"] == ["name"]

    human = runner.invoke(cli, ["db", "query", database_id, "SELECT name FROM widgets ORDER BY id"])
    assert human.exit_code == 0, human.output
    assert "sprocket" in human.output
    assert "cog" in human.output

    destroyed = runner.invoke(cli, ["db", "destroy", database_id, "--yes"])
    assert destroyed.exit_code == 0, destroyed.output
    assert "Destroyed" in destroyed.output

    gone = runner.invoke(cli, ["db", "list", "--json"])
    assert database_id not in [entry["database_id"] for entry in json.loads(gone.output)]


def test_db_query_rejects_unsafe_sql(
    runner: CliRunner, tmp_path: Path, sample_db_env: Path
) -> None:
    database_id = _create(runner, tmp_path)
    result = runner.invoke(cli, ["db", "query", database_id, "DROP TABLE widgets"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output


def test_db_destroy_missing_database_is_idempotent(runner: CliRunner, sample_db_env: Path) -> None:
    random_id = str(uuid.uuid4())
    result = runner.invoke(cli, ["db", "destroy", random_id, "--yes"])
    assert result.exit_code == 0
    assert "nothing to do" in result.output


def test_db_bad_uuid_fails_cleanly(runner: CliRunner, sample_db_env: Path) -> None:
    result = runner.invoke(cli, ["db", "query", "not-a-uuid", "SELECT 1"])
    assert result.exit_code == 1
    assert "not a valid database id" in result.output
    assert "Traceback" not in result.output


def test_db_query_denies_catalog_access_by_default(
    runner: CliRunner, tmp_path: Path, sample_db_env: Path
) -> None:
    """D12: sqlite_master is denied unless --allow-catalog is passed."""
    database_id = _create(runner, tmp_path)

    denied = runner.invoke(cli, ["db", "query", database_id, "SELECT sql FROM sqlite_master"])
    assert denied.exit_code == 1
    assert "D12" in denied.output or "catalog" in denied.output.lower()

    allowed = runner.invoke(
        cli,
        ["db", "query", database_id, "SELECT sql FROM sqlite_master", "--allow-catalog", "--json"],
    )
    assert allowed.exit_code == 0, allowed.output


def test_db_create_rejects_non_sqlite_dialect(
    runner: CliRunner, tmp_path: Path, sample_db_env: Path
) -> None:
    schema_path = tmp_path / "schema.sql"
    schema_path.write_text(DDL, encoding="utf-8")
    result = runner.invoke(
        cli, ["db", "create", "--schema", str(schema_path), "--dialect", "postgres"]
    )
    assert result.exit_code == 1
    assert "sqlite" in result.output
