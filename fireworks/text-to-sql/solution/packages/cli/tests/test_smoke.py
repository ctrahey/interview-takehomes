"""Smoke test: t2s_cli is importable and its click group builds correctly."""

from click.testing import CliRunner

from t2s_cli.cli import cli


def test_cli_help() -> None:
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "text-to-SQL" in result.output


def test_cli_help_lists_all_design_commands() -> None:
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    for name in ("query", "schema", "db", "eval"):
        assert name in result.output


def test_db_help_lists_all_subcommands() -> None:
    result = CliRunner().invoke(cli, ["db", "--help"])
    assert result.exit_code == 0
    for name in ("create", "load", "query", "list", "destroy"):
        assert name in result.output
