"""D9: a client-supplied path must never reach the filesystem.

These tests attack `foundation.paths.database_path` directly, since it is
the single choke point every sample-database file path goes through.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from foundation.paths import database_path, managed_directory


def test_database_path_lives_under_the_managed_directory() -> None:
    database_id = uuid.uuid4()
    path = database_path(database_id)
    assert path.parent == managed_directory()
    assert path.name == f"{database_id.hex}.sqlite3"


def test_database_path_rejects_non_uuid_input_by_type() -> None:
    # The function signature demands a uuid.UUID, not a str -- there is no
    # parameter a raw path-traversal string could be smuggled through.
    with pytest.raises(TypeError):
        database_path("../../../../etc/passwd")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "malicious_text",
    [
        "../../../../etc/passwd",
        "../../secrets",
        "/etc/passwd",
        "..\\..\\windows\\system32",
        "a" * 5 + "; rm -rf /",
    ],
)
def test_malicious_text_cannot_even_be_turned_into_a_uuid(malicious_text: str) -> None:
    # Defense in depth, layer two: even if a caller tried to route a
    # malicious string through `uuid.UUID(...)` first (the only way to reach
    # `database_path` at all), the constructor itself rejects it -- these
    # strings are not valid UUID text, so no path-traversal payload can ever
    # be represented as a `uuid.UUID` in the first place.
    with pytest.raises(ValueError):
        uuid.UUID(malicious_text)


def test_two_different_uuids_never_collide_or_traverse() -> None:
    a = database_path(uuid.uuid4())
    b = database_path(uuid.uuid4())
    assert a != b
    assert a.resolve().parent == managed_directory().resolve()
    assert b.resolve().parent == managed_directory().resolve()


def test_managed_directory_is_overridable_for_tests(managed_sample_db_dir: Path) -> None:
    # `managed_sample_db_dir` (conftest) points T2S_SAMPLE_DB_DIR at a temp dir.
    assert managed_directory() == managed_sample_db_dir
