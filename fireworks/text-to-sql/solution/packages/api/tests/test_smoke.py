"""Smoke test: t2s_api is importable and the workspace is wired correctly."""

import t2s_api


def test_package_importable() -> None:
    assert t2s_api.__doc__ is not None
