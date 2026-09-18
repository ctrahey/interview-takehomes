"""``t2s schema`` — offline, replayed against t2s_core's shipped fixtures."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from t2s_cli.cli import cli
from t2s_core.fixtures.scenarios import SCHEMA_SCENARIOS, SchemaScenario


def _scenario(name: str) -> SchemaScenario:
    return next(s for s in SCHEMA_SCENARIOS if s.name == name)


def test_schema_valid_writes_ddl_with_out(runner: CliRunner, tmp_path: Path) -> None:
    scenario = _scenario("library_lending")
    out_path = tmp_path / "out.sql"
    result = runner.invoke(
        cli,
        ["--offline", "schema", "--describe", scenario.request.description, "--out", str(out_path)],
    )
    assert result.exit_code == 0, result.output
    assert out_path.exists()
    assert "CREATE TABLE" in out_path.read_text(encoding="utf-8").upper()
    assert f"Wrote DDL to {out_path}" in result.output
    assert "Result: VALID" in result.output


def test_schema_clarification_needed_exits_two(runner: CliRunner) -> None:
    scenario = _scenario("too_thin")
    result = runner.invoke(cli, ["--offline", "schema", "--describe", scenario.request.description])
    assert result.exit_code == 2, result.output
    assert "CLARIFICATION" in result.output


def test_schema_json_output(runner: CliRunner) -> None:
    scenario = _scenario("library_lending")
    result = runner.invoke(
        cli, ["--offline", "schema", "--describe", scenario.request.description, "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["response_class"] == "valid"
    assert "CREATE TABLE" in payload["query"].upper()
