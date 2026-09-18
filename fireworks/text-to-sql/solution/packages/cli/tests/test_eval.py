"""``t2s eval run`` -- thin delegation to evals/harness (W4), if it exists.

These tests deliberately never let a subprocess actually start ``python -m
evals.harness``: that harness makes *live* Fireworks calls by default (see
``evals/harness/__main__.py``'s ``cmd_run``, which falls back to a real API
key from ``~/.fireworks-key`` exactly like ``make eval`` does) unless the
caller passes its own ``--fake``. A CLI test suite must stay offline (D8), so
every test here points ``_repo_root()`` at an isolated temp directory and, for
the "harness present" case, stubs ``subprocess.run`` instead of letting one
actually spawn.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import t2s_cli.cli as cli_module
from t2s_cli.cli import cli


def test_eval_run_without_harness_fails_cleanly(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "packages").mkdir()
    (tmp_path / "evals" / "harness").mkdir(parents=True)
    monkeypatch.setattr(cli_module, "_repo_root", lambda: tmp_path)

    result = runner.invoke(cli, ["eval", "run"])
    assert result.exit_code == 1
    assert "eval harness not available" in result.output
    assert "Traceback" not in result.output


def test_eval_run_delegates_when_harness_is_present(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a runner module exists, the CLI must invoke it via subprocess with
    the harness's own argv, forwarded verbatim, and propagate its exit code.
    ``subprocess.run`` is stubbed so this never spawns a real (live-API)
    process."""
    (tmp_path / "packages").mkdir()
    harness_dir = tmp_path / "evals" / "harness"
    harness_dir.mkdir(parents=True)
    (harness_dir / "__main__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(cli_module, "_repo_root", lambda: tmp_path)

    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], *, cwd: Path, check: bool) -> subprocess.CompletedProcess[bytes]:
        captured["argv"] = argv
        captured["cwd"] = cwd
        return subprocess.CompletedProcess(argv, returncode=0)

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    result = runner.invoke(cli, ["eval", "run", "--fake", "--limit", "1"])
    assert result.exit_code == 0, result.output
    argv = captured["argv"]
    assert argv[-3:] == ["--fake", "--limit", "1"]
    assert argv[:3] == [cli_module.sys.executable, "-m", "evals.harness"]
    assert captured["cwd"] == tmp_path
