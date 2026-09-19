"""Versioned prompt templates for layer 3.

Deliberately reuses ``t2s_core.prompts.registry.PromptRegistry`` -- the file
naming, strict substitution and pinning behaviour are already right, and
duplicating them here would mean two rollout mechanisms to keep honest. Only the
directory and the pins are ours.
"""

from __future__ import annotations

from pathlib import Path

from t2s_core.prompts.registry import PromptRegistry

__all__ = ["PINS", "REGISTRY", "TEMPLATE_DIR"]

TEMPLATE_DIR = Path(__file__).parent / "templates"

PINS: dict[str, str] = {
    "router.system": "v2",
    "router.user": "v1",
    "router.state": "v1",
    "data.system": "v1",
    "data.user": "v1",
}

REGISTRY = PromptRegistry(TEMPLATE_DIR, PINS)
