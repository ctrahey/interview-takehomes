"""Parse + transpile validation for dialects we never execute (D5).

Postgres and MySQL are supported as *generation targets*. We do not claim to
bind-check them: this validator proves the statement parses in the target
dialect, passes the D9 gate, and round-trips through sqlglot. It catches syntax
errors and dialect mistakes; it cannot catch an unknown column.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot.errors import SqlglotError

from t2s_core.errors import SafetyViolation
from t2s_core.models import Verdict
from t2s_core.validation.safety import assert_ddl_only, assert_read_only_select

__all__ = ["SqlglotValidator"]


@dataclass(slots=True)
class SqlglotValidator:
    @property
    def name(self) -> str:
        return "sqlglot"

    def check(self, sql: str, schema_ddl: str, dialect: str) -> Verdict:
        try:
            tree = assert_read_only_select(sql, dialect)
        except SafetyViolation as exc:
            return Verdict.failed("safety_gate", exc.message, code=exc.code)
        try:
            sqlglot.transpile(tree.sql(dialect=dialect), read=dialect, write=dialect)
        except SqlglotError as exc:
            return Verdict.failed("binder", f"{dialect} transpile failed: {exc}")
        return Verdict.passed(stage="parse")

    def check_ddl(self, ddl: str, dialect: str) -> Verdict:
        try:
            statements = assert_ddl_only(ddl, dialect)
        except SafetyViolation as exc:
            return Verdict.failed("safety_gate", exc.message, code=exc.code)
        try:
            for statement in statements:
                sqlglot.transpile(statement.sql(dialect=dialect), read=dialect, write=dialect)
        except SqlglotError as exc:
            return Verdict.failed("binder", f"{dialect} transpile failed: {exc}")
        return Verdict.passed(tables=len(statements))
