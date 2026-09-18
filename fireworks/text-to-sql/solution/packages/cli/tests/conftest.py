"""Shared fixtures for the offline CLI test suite (CliRunner, no network)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from a known environment, regardless of what the host
    shell (or the Makefile's ``load_key``) happens to export."""
    for name in ("FIREWORKS_API_KEY", "T2S_OFFLINE", "NO_COLOR", "T2S_MODEL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def sample_db_env(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point foundation's sample-database lifecycle at a throwaway directory."""
    directory = tmp_path_factory.mktemp("sample_dbs")
    monkeypatch.setenv("T2S_SAMPLE_DB_DIR", str(directory))
    return directory
