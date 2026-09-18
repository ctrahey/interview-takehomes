"""Smoke test: foundation is importable and the workspace is wired correctly."""

import foundation


def test_package_importable() -> None:
    assert foundation.__doc__ is not None
