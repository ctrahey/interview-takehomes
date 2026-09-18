"""``t2s query`` — offline, replayed against t2s_core's shipped fixtures (D8).

The headline test here is ``test_repair_loop_shows_both_attempts``: it is the
money demo (rejected candidate → engine error → fix), asserted against the
actual rendered output so a regression in legibility fails the suite, not just
a human squinting at a terminal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from t2s_cli.cli import cli
from t2s_core.fixtures.scenarios import QUERY_SCENARIOS, REPAIR_DEMOS, RepairDemo, Scenario


def _scenario(name: str) -> Scenario:
    return next(s for s in QUERY_SCENARIOS if s.name == name)


def _demo(name: str) -> RepairDemo:
    return next(d for d in REPAIR_DEMOS if d.name == name)


def _write_schema(tmp_path: Path, ddl: str) -> str:
    path = tmp_path / "schema.sql"
    path.write_text(ddl, encoding="utf-8")
    return str(path)


def test_valid_query_exits_zero(runner: CliRunner, tmp_path: Path) -> None:
    scenario = _scenario("simple_count")
    schema_path = _write_schema(tmp_path, scenario.request.schema_ddl)
    result = runner.invoke(
        cli,
        ["--offline", "query", "--schema", schema_path, "--question", scenario.request.question],
    )
    assert result.exit_code == 0, result.output
    assert "Result: VALID" in result.output
    assert "SELECT" in result.output.upper()


def test_clarification_needed_exits_two(runner: CliRunner, tmp_path: Path) -> None:
    scenario = _scenario("ambiguous_best_customers")
    schema_path = _write_schema(tmp_path, scenario.request.schema_ddl)
    result = runner.invoke(
        cli,
        ["--offline", "query", "--schema", schema_path, "--question", scenario.request.question],
    )
    assert result.exit_code == 2, result.output
    assert "CLARIFICATION" in result.output


def test_error_exits_three(runner: CliRunner, tmp_path: Path) -> None:
    scenario = _scenario("unanswerable_salaries")
    schema_path = _write_schema(tmp_path, scenario.request.schema_ddl)
    result = runner.invoke(
        cli,
        ["--offline", "query", "--schema", schema_path, "--question", scenario.request.question],
    )
    assert result.exit_code == 3, result.output
    assert "Result: ERROR" in result.output


def test_repair_loop_shows_both_attempts(runner: CliRunner, tmp_path: Path) -> None:
    """The money demo: attempt 1 rejected with the engine's exact error,
    attempt 2 accepted with the fix -- all of it visible in one render."""
    demo = _demo("repair_demo_wrong_column_name")
    schema_path = _write_schema(tmp_path, demo.request.schema_ddl)
    result = runner.invoke(
        cli, ["--offline", "query", "--schema", schema_path, "--question", demo.request.question]
    )
    assert result.exit_code == 0, result.output
    assert "Attempt 1/2" in result.output
    assert "REJECTED" in result.output
    assert "signup_date" in result.output  # the rejected candidate's wrong column
    assert "no such column" in result.output  # the engine's exact error
    assert "Attempt 2/2" in result.output
    assert "ACCEPTED" in result.output
    assert "signed_up" in result.output  # the fix
    assert "Result: VALID" in result.output
    assert "repairs_used=1" in result.output


def test_no_repair_flag_skips_validation(runner: CliRunner, tmp_path: Path) -> None:
    """D4's loop-off arm: without validation, the broken candidate is returned
    as the final answer instead of being caught and repaired."""
    demo = _demo("repair_demo_wrong_column_name")
    schema_path = _write_schema(tmp_path, demo.request.schema_ddl)
    result = runner.invoke(
        cli,
        [
            "--offline",
            "query",
            "--schema",
            schema_path,
            "--question",
            demo.request.question,
            "--no-repair",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Attempt 2" not in result.output
    assert "signup_date" in result.output  # the wrong column, uncaught


def test_json_output_is_a_clean_envelope(runner: CliRunner, tmp_path: Path) -> None:
    scenario = _scenario("simple_count")
    schema_path = _write_schema(tmp_path, scenario.request.schema_ddl)
    result = runner.invoke(
        cli,
        [
            "--offline",
            "query",
            "--schema",
            schema_path,
            "--question",
            scenario.request.question,
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    # --json means the raw envelope and nothing else on *stdout* -- the
    # offline banner is diagnostic and goes to stderr, not stdout, precisely
    # so it never contaminates scripted output.
    payload = json.loads(result.stdout)
    assert payload["response_class"] == "valid"
    assert payload["query"]
    assert "attempts" in payload["metadata"]
    assert result.stdout.count("\n") == 1
    assert "[offline]" in result.stderr


def test_missing_schema_file_fails_cleanly(runner: CliRunner) -> None:
    result = runner.invoke(
        cli, ["--offline", "query", "--schema", "/no/such/file.sql", "--question", "how many?"]
    )
    assert result.exit_code == 1
    assert "not found" in result.output
    assert "Traceback" not in result.output


def test_missing_api_key_fails_cleanly_without_offline(runner: CliRunner, tmp_path: Path) -> None:
    scenario = _scenario("simple_count")
    schema_path = _write_schema(tmp_path, scenario.request.schema_ddl)
    result = runner.invoke(
        cli, ["query", "--schema", schema_path, "--question", scenario.request.question]
    )
    assert result.exit_code == 1
    assert "FIREWORKS_API_KEY" in result.output
    assert "Traceback" not in result.output


def test_t2s_offline_env_var_is_equivalent_to_the_flag(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("T2S_OFFLINE", "1")
    scenario = _scenario("simple_count")
    schema_path = _write_schema(tmp_path, scenario.request.schema_ddl)
    result = runner.invoke(
        cli, ["query", "--schema", schema_path, "--question", scenario.request.question]
    )
    assert result.exit_code == 0, result.output
