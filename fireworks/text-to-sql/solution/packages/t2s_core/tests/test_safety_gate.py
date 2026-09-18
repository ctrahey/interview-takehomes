"""D9 safety gate — the adversarial evidence for the submission.

Each rejection below is a distinct attack class, and every one of them is checked
on the sqlglot AST. The three "must still pass" cases at the bottom matter just as
much: they are the ones a naive string matcher gets wrong in the other direction.
"""

from __future__ import annotations

import pytest

from t2s_core.errors import SafetyViolation
from t2s_core.validation.safety import assert_ddl_only, assert_read_only_select

REJECTED = [
    ("multi_statement", "SELECT 1; DROP TABLE customers", "multiple_statements"),
    ("multi_statement_benign", "SELECT 1; SELECT 2", "multiple_statements"),
    ("drop", "DROP TABLE customers", "forbidden_statement"),
    ("delete", "DELETE FROM customers", "forbidden_statement"),
    ("update", "UPDATE customers SET city = 'x'", "forbidden_statement"),
    ("insert", "INSERT INTO customers VALUES (1, 'a', 'b')", "forbidden_statement"),
    ("create", "CREATE TABLE evil (a INTEGER)", "forbidden_statement"),
    ("alter", "ALTER TABLE customers ADD COLUMN evil TEXT", "forbidden_statement"),
    ("pragma", "PRAGMA table_info(customers)", "forbidden_statement"),
    ("pragma_writable", "PRAGMA query_only=OFF", "forbidden_statement"),
    ("attach", "ATTACH DATABASE '/etc/passwd' AS pw", "forbidden_statement"),
    ("detach", "DETACH DATABASE pw", "forbidden_statement"),
    ("vacuum_as_command", "VACUUM", "forbidden_statement"),
    ("explain_as_command", "EXPLAIN SELECT 1", "forbidden_statement"),
    ("transaction", "BEGIN TRANSACTION", "forbidden_statement"),
    (
        # The case that kills naive root-node checks: this parses as a top-level
        # SELECT with an INSERT hidden in the CTE.
        "insert_smuggled_in_cte",
        "WITH x AS (INSERT INTO customers VALUES (1,'a','b') RETURNING *) SELECT * FROM x",
        "forbidden_statement",
    ),
    ("load_extension", "SELECT load_extension('/tmp/evil.so')", "forbidden_function"),
    ("readfile", "SELECT readfile('/etc/passwd')", "forbidden_function"),
    ("empty", "   ", "empty_statement"),
    ("unparseable", "SELECT FROM WHERE ((", "unparseable"),
]


@pytest.mark.parametrize(("label", "sql", "code"), REJECTED, ids=[c[0] for c in REJECTED])
def test_query_gate_rejects(label: str, sql: str, code: str) -> None:
    with pytest.raises(SafetyViolation) as caught:
        assert_read_only_select(sql, "sqlite")
    assert caught.value.code == code


ACCEPTED = [
    ("plain_select", "SELECT name FROM customers"),
    ("lowercase", "select name from customers"),
    ("with_cte", "WITH t AS (SELECT 1 AS a) SELECT a FROM t"),
    ("union", "SELECT 1 UNION SELECT 2"),
    ("except", "SELECT 1 EXCEPT SELECT 2"),
    ("subquery_root", "(SELECT 1)"),
    ("window_function", "SELECT name, row_number() OVER (ORDER BY name) FROM customers"),
    # Not a rejection: the "DROP TABLE" here is inside a comment and inside a
    # string literal. A regex-based gate fails both of these.
    ("drop_in_comment", "SELECT name FROM customers -- DROP TABLE customers"),
    ("drop_in_literal", "SELECT 'DROP TABLE customers' AS spooky"),
    ("trailing_semicolon", "SELECT 1;"),
]


@pytest.mark.parametrize(("label", "sql"), ACCEPTED, ids=[c[0] for c in ACCEPTED])
def test_query_gate_accepts(label: str, sql: str) -> None:
    assert assert_read_only_select(sql, "sqlite") is not None


DDL_REJECTED = [
    ("drop", "DROP TABLE customers", "forbidden_statement"),
    ("insert", "CREATE TABLE a (x INT); INSERT INTO a VALUES (1)", "forbidden_statement"),
    ("attach", "ATTACH DATABASE '/etc/passwd' AS pw", "forbidden_statement"),
    ("pragma", "PRAGMA journal_mode=WAL", "forbidden_statement"),
    ("select", "SELECT 1", "forbidden_statement"),
    ("empty", "", "empty_statement"),
]


@pytest.mark.parametrize(("label", "ddl", "code"), DDL_REJECTED, ids=[c[0] for c in DDL_REJECTED])
def test_ddl_gate_rejects(label: str, ddl: str, code: str) -> None:
    with pytest.raises(SafetyViolation) as caught:
        assert_ddl_only(ddl, "sqlite")
    assert caught.value.code == code


def test_ddl_gate_accepts_create_statements() -> None:
    ddl = """
    CREATE TABLE a (x INTEGER PRIMARY KEY);
    CREATE INDEX idx_a_x ON a (x);
    CREATE VIEW v AS SELECT x FROM a;
    """
    assert len(assert_ddl_only(ddl, "sqlite")) == 3
