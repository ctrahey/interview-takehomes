"""Validator behaviour, including the D9 execution-side guarantees."""

from __future__ import annotations

import sqlite3

import pytest

from t2s_core.validation import (
    EphemeralSqliteValidator,
    NoOpValidator,
    SqlglotValidator,
    default_validator,
)

INFINITE = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT x FROM c"


def test_accepts_a_correct_join(retail_ddl: str) -> None:
    sql = (
        "SELECT c.name, COUNT(o.order_id) AS n FROM customers c "
        "JOIN orders o ON o.customer_id = c.customer_id GROUP BY c.name"
    )
    verdict = EphemeralSqliteValidator().check(sql, retail_ddl, "sqlite")
    assert verdict.ok, verdict.error


def test_catches_unknown_column(retail_ddl: str) -> None:
    verdict = EphemeralSqliteValidator().check("SELECT email FROM customers", retail_ddl, "sqlite")
    assert not verdict.ok
    assert verdict.kind == "binder"
    assert "no such column: email" in (verdict.error or "")


def test_catches_unknown_table(retail_ddl: str) -> None:
    verdict = EphemeralSqliteValidator().check("SELECT * FROM employees", retail_ddl, "sqlite")
    assert not verdict.ok
    assert "no such table: employees" in (verdict.error or "")


def test_safety_violation_is_reported_as_such(retail_ddl: str) -> None:
    verdict = EphemeralSqliteValidator().check(
        "SELECT 1; DROP TABLE customers", retail_ddl, "sqlite"
    )
    assert not verdict.ok
    assert verdict.kind == "safety_gate"


def test_unusable_request_ddl_is_reported_not_raised() -> None:
    verdict = EphemeralSqliteValidator().check("SELECT 1", "ATTACH DATABASE 'x' AS y", "sqlite")
    assert not verdict.ok
    assert "schema DDL is unusable" in (verdict.error or "")


def test_row_cap_bounds_an_unbounded_query(retail_ddl: str) -> None:
    """An infinite recursive CTE returns, capped, instead of hanging."""
    verdict = EphemeralSqliteValidator(row_cap=10).check(INFINITE, retail_ddl, "sqlite")
    assert verdict.ok
    assert verdict.detail["rows_sampled"] == 10


def test_wall_clock_timeout_interrupts(retail_ddl: str) -> None:
    """A query that produces no rows until it finishes cannot run forever."""
    sql = (
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 100000000) "
        "SELECT COUNT(*) FROM c"
    )
    verdict = EphemeralSqliteValidator(timeout_s=0.2).check(sql, retail_ddl, "sqlite")
    assert not verdict.ok
    assert "did not finish" in (verdict.error or "")


def test_connection_is_read_only_even_if_the_gate_were_bypassed(retail_ddl: str) -> None:
    """Defense in depth: PRAGMA query_only=ON on the validation connection."""
    connection = EphemeralSqliteValidator()._build(retail_ddl)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO customers VALUES (1, 'a', 'b')")
    finally:
        connection.close()


def test_bind_only_mode_does_not_execute(retail_ddl: str) -> None:
    verdict = EphemeralSqliteValidator(execute=False).check(INFINITE, retail_ddl, "sqlite")
    assert verdict.ok
    assert verdict.detail["stage"] == "bind"


def test_sqlglot_validator_parses_but_does_not_bind(retail_ddl: str) -> None:
    validator = SqlglotValidator()
    assert validator.check("SELECT email FROM customers", retail_ddl, "postgres").ok
    assert not validator.check("SELECT FROM ((", retail_ddl, "postgres").ok
    assert not validator.check("DROP TABLE customers", retail_ddl, "postgres").ok


def test_noop_validator_passes_everything(retail_ddl: str) -> None:
    """The loop-off arm of the eval: even a rejected statement passes."""
    assert NoOpValidator().check("SELECT nope FROM nowhere", retail_ddl, "sqlite").ok
    assert NoOpValidator().check_ddl("DROP TABLE x", "sqlite").ok


def test_default_validator_per_dialect() -> None:
    assert isinstance(default_validator("sqlite"), EphemeralSqliteValidator)
    assert isinstance(default_validator("postgres"), SqlglotValidator)


def test_schema_validator_executes_ddl() -> None:
    validator = EphemeralSqliteValidator()
    assert validator.check_ddl("CREATE TABLE a (x INTEGER PRIMARY KEY)", "sqlite").ok
    bad = validator.check_ddl("CREATE TABLE a (x INTEGER PRIMARY KEY, x TEXT)", "sqlite")
    assert not bad.ok
    assert "duplicate column" in (bad.error or "")
