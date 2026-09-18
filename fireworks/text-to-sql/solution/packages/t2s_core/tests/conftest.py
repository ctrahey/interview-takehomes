"""Test configuration. Nothing here touches the network (D8).

The path hook makes ``tests/doubles.py`` importable under pytest's importlib
import mode, where test modules are not part of a package.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from doubles import RETAIL_DDL  # noqa: E402


@pytest.fixture
def retail_ddl() -> str:
    return RETAIL_DDL
