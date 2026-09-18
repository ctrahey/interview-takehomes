"""Unit tests for `foundation.security`: the D9 AST-based safety gate."""

from __future__ import annotations

import pytest

from foundation.security import CatalogAccessDeniedError, UnsafeQueryError, assert_safe_select


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t",
        "SELECT a, b FROM t WHERE a > 1 ORDER BY b LIMIT 10",
        "WITH x AS (SELECT 1 AS a) SELECT * FROM x",
        "SELECT 1 UNION SELECT 2",
        "SELECT 1 INTERSECT SELECT 2",
        "SELECT 1 EXCEPT SELECT 2",
    ],
)
def test_allows_select_and_with_statements(sql: str) -> None:
    assert_safe_select(sql, "sqlite")  # does not raise


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE t",
        "DELETE FROM t",
        "UPDATE t SET a = 1",
        "INSERT INTO t VALUES (1)",
        "CREATE TABLE t (a INT)",
        "ALTER TABLE t ADD COLUMN b INT",
        "PRAGMA table_info(t)",
        "ATTACH DATABASE 'evil.db' AS evil",
        "VACUUM",
    ],
)
def test_rejects_non_select_statement_kinds(sql: str) -> None:
    with pytest.raises(UnsafeQueryError):
        assert_safe_select(sql, "sqlite")


def test_rejects_stacked_statements() -> None:
    with pytest.raises(UnsafeQueryError, match="single statement"):
        assert_safe_select("SELECT 1; DROP TABLE t;", "sqlite")


def test_rejects_stacked_statements_even_when_first_looks_safe() -> None:
    with pytest.raises(UnsafeQueryError, match="single statement"):
        assert_safe_select("SELECT * FROM t; DELETE FROM t;", "sqlite")


def test_rejects_empty_query() -> None:
    with pytest.raises(UnsafeQueryError, match="empty"):
        assert_safe_select("", "sqlite")


def test_rejects_unparseable_query() -> None:
    with pytest.raises(UnsafeQueryError, match="does not parse"):
        assert_safe_select("SELECT FROM WHERE ]]] not sql", "sqlite")


def test_rejection_is_never_by_substring_matching() -> None:
    # A column literally named "drop_table" must not be rejected just
    # because the disallowed keyword appears as a substring anywhere in the
    # text -- the gate is AST-based, not a string/regex denylist.
    assert_safe_select("SELECT drop_table_flag FROM audit_log", "sqlite")


# ---------------------------------------------------------------------------
# D12 -- catalog access is denied by default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT sql FROM sqlite_master",
        "SELECT name FROM sqlite_master WHERE type = 'table'",
        "select * from SQLITE_MASTER",  # case-insensitive identifier
        "SELECT * FROM sqlite_schema",
        "SELECT * FROM main.sqlite_master",
        "WITH x AS (SELECT sql FROM sqlite_master) SELECT * FROM x",
        "SELECT (SELECT sql FROM sqlite_master LIMIT 1)",
    ],
)
def test_catalog_access_denied_by_default(sql: str) -> None:
    with pytest.raises(CatalogAccessDeniedError):
        assert_safe_select(sql, "sqlite")


def test_catalog_access_denial_is_a_kind_of_unsafe_query_error() -> None:
    # Callers that only handle the general case still catch it.
    with pytest.raises(UnsafeQueryError):
        assert_safe_select("SELECT sql FROM sqlite_master", "sqlite")


def test_catalog_access_permitted_with_explicit_opt_in() -> None:
    assert_safe_select("SELECT sql FROM sqlite_master", "sqlite", allow_catalog=True)


def test_catalog_denylist_is_not_confused_by_a_table_merely_named_similarly() -> None:
    # A user table that happens to contain "sqlite_master" as a substring of
    # a *different* identifier must not trip the gate -- it's AST-based table
    # identity, not substring matching.
    assert_safe_select("SELECT * FROM my_sqlite_master_backup", "sqlite")
