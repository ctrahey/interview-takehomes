"""Smoke test: t2s_core is importable and the workspace is wired correctly."""

import t2s_core


def test_package_importable() -> None:
    assert t2s_core.__doc__ is not None
