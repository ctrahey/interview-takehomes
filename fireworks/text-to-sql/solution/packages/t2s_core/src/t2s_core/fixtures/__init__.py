"""Recorded inference fixtures, shipped inside the package.

Shipping them here (rather than under ``tests/``) is deliberate: it means the
CLI, the API's behavioural suite, and the eval harness can all demonstrate the
full pipeline with real model output and no network and no API key (D8).
"""

from __future__ import annotations

from pathlib import Path

from t2s_core.clients import RecordedClient
from t2s_core.config import DEFAULT_MODEL

__all__ = ["FIXTURE_DIR", "recorded_client"]

FIXTURE_DIR = Path(__file__).parent / "inference"


def recorded_client(model: str = DEFAULT_MODEL) -> RecordedClient:
    """An offline InferenceClient replaying the committed fixtures."""
    return RecordedClient(FIXTURE_DIR, model=model)
