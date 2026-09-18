"""Integration tests for the sample database lifecycle: create -> load -> query -> destroy (D9)."""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from foundation import sample_db
from foundation.ddl import render_ddl
from foundation.graph import Column, EntityGraph, Table
from foundation.security import CatalogAccessDeniedError, UnsafeQueryError

_GRAPH = EntityGraph(
    tables=[
        Table(
            name="widgets",
            columns=[
                Column(name="id", type="INT", nullable=False),
                Column(name="name", type="TEXT", nullable=False),
                Column(name="price", type="INT"),
            ],
            primary_key=["id"],
        )
    ]
)
_DDL = render_ddl(_GRAPH, "sqlite")


def test_full_lifecycle_create_load_query_destroy() -> None:
    database_id = sample_db.create(_DDL)
    assert sample_db.exists(database_id)

    inserted = sample_db.load(
        database_id,
        {
            "widgets": [
                {"id": 1, "name": "sprocket", "price": 10},
                {"id": 2, "name": "gizmo", "price": 20},
            ]
        },
    )
    assert inserted == 2

    result = sample_db.query(database_id, "SELECT id, name FROM widgets ORDER BY id")
    assert result.columns == ["id", "name"]
    assert result.rows == [(1, "sprocket"), (2, "gizmo")]
    assert result.truncated is False

    sample_db.destroy(database_id)
    assert not sample_db.exists(database_id)


def test_query_against_missing_database_raises() -> None:
    with pytest.raises(sample_db.DatabaseNotFoundError):
        sample_db.query(uuid.uuid4(), "SELECT 1")


def test_destroy_is_idempotent() -> None:
    database_id = sample_db.create(_DDL)
    sample_db.destroy(database_id)
    sample_db.destroy(database_id)  # must not raise


def test_create_rejects_non_executable_dialect() -> None:
    with pytest.raises(ValueError, match="dialect"):
        sample_db.create(_DDL, dialect="postgres")


# ---------------------------------------------------------------------------
# D9 security guarantees
# ---------------------------------------------------------------------------


def test_write_attempt_through_query_is_rejected() -> None:
    database_id = sample_db.create(_DDL)
    with pytest.raises(UnsafeQueryError):
        sample_db.query(database_id, "DELETE FROM widgets")

    with pytest.raises(UnsafeQueryError):
        sample_db.query(database_id, "INSERT INTO widgets VALUES (99, 'hacked', 0)")

    with pytest.raises(UnsafeQueryError):
        sample_db.query(database_id, "SELECT 1; DROP TABLE widgets; ")

    # Prove the rejection actually happened before touching the DB: the table
    # still has zero rows, and a legitimate read-only query still works.
    result = sample_db.query(database_id, "SELECT count(*) AS n FROM widgets")
    assert result.rows == [(0,)]


def test_readonly_connection_physically_blocks_writes_even_bypassing_the_ast_gate() -> None:
    """Layer 2 of the defense: even a raw write against the read-only connection fails.

    This does not go through `sample_db.query()` (which would reject it at
    the AST gate first) -- it opens the same kind of read-only connection
    `query()` uses and attempts a write directly, to prove the physical
    read-only guarantee holds independently of the AST allowlist.
    """
    database_id = sample_db.create(_DDL)
    path = sample_db._existing_path(database_id)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO widgets VALUES (1, 'x', 1)")
    finally:
        conn.close()


def test_query_timeout_triggers_on_a_runaway_query() -> None:
    database_id = sample_db.create(_DDL)
    runaway = (
        "WITH RECURSIVE counter(x) AS "
        "(SELECT 1 UNION ALL SELECT x + 1 FROM counter WHERE x < 100000000) "
        "SELECT count(*) FROM counter"
    )
    with pytest.raises(sample_db.QueryTimeoutError):
        sample_db.query(database_id, runaway, timeout_seconds=0.05)


def test_row_cap_truncates_large_result_sets() -> None:
    database_id = sample_db.create(_DDL)
    rows = [{"id": i, "name": f"item-{i}", "price": i} for i in range(1, 51)]
    sample_db.load(database_id, {"widgets": rows})

    result = sample_db.query(database_id, "SELECT id FROM widgets ORDER BY id", row_cap=10)
    assert result.row_count == 10
    assert result.truncated is True
    assert [r[0] for r in result.rows] == list(range(1, 11))


def test_row_cap_not_triggered_when_result_fits() -> None:
    database_id = sample_db.create(_DDL)
    sample_db.load(database_id, {"widgets": [{"id": 1, "name": "x", "price": 1}]})
    result = sample_db.query(database_id, "SELECT id FROM widgets", row_cap=10)
    assert result.truncated is False


def test_catalog_access_denied_by_default_against_a_real_sample_database() -> None:
    database_id = sample_db.create(_DDL)
    with pytest.raises(CatalogAccessDeniedError):
        sample_db.query(database_id, "SELECT sql FROM sqlite_master")


def test_catalog_access_permitted_with_explicit_opt_in_against_a_real_sample_database() -> None:
    database_id = sample_db.create(_DDL)
    result = sample_db.query(
        database_id, "SELECT name FROM sqlite_master WHERE type = 'table'", allow_catalog=True
    )
    assert "widgets" in {row[0] for row in result.rows}


def test_load_validates_identifiers_against_injection() -> None:
    database_id = sample_db.create(_DDL)
    with pytest.raises(sample_db.InvalidIdentifierError):
        sample_db.load(database_id, {"widgets; DROP TABLE widgets;--": [{"id": 1}]})
    with pytest.raises(sample_db.InvalidIdentifierError):
        sample_db.load(database_id, {"widgets": [{"id; DROP TABLE widgets;--": 1}]})
