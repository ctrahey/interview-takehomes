"""Smoke test: t2s_cli is importable and its click group builds correctly."""

from click.testing import CliRunner

from t2s_cli.cli import cli


def test_cli_help() -> None:
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "text-to-SQL" in result.output
