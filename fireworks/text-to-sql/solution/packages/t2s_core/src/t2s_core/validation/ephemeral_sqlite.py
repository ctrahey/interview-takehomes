"""The default validator: an ephemeral in-memory SQLite database built from the
DDL *in the request* (D4).

``sqlite3`` is stdlib, so this is literally zero dependency on ``foundation`` --
the import-linter contract is satisfied by construction, not by discipline.

Sequence for every candidate:

1. Parse and gate the request's DDL (CREATE-only; a request whose "schema" is
   really an ATTACH is refused before anything executes).
2. Build the schema in ``:memory:``.
3. ``PRAGMA query_only=ON`` — the connection cannot write even if the gate is
   wrong. Defense in depth, D9.
4. Run the read-only gate on the candidate.
5. ``EXPLAIN`` to bind-check (unknown table, unknown column, arity errors), then
   optionally execute under a wall-clock timeout with a row cap.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from t2s_core.errors import SafetyViolation
from t2s_core.models import Verdict
from t2s_core.validation.safety import assert_ddl_only, assert_read_only_select

__all__ = ["EphemeralSqliteValidator"]

DEFAULT_TIMEOUT_S = 2.0
DEFAULT_ROW_CAP = 100


@dataclass(slots=True)
class EphemeralSqliteValidator:
    """Implements both :class:`~t2s_core.ports.QueryValidator` and
    :class:`~t2s_core.ports.SchemaValidator`."""

    timeout_s: float = DEFAULT_TIMEOUT_S
    row_cap: int = DEFAULT_ROW_CAP
    #: When False, stop after the EXPLAIN bind-check and never run the query.
    execute: bool = True

    @property
    def name(self) -> str:
        return "ephemeral_sqlite"

    # -- ports.QueryValidator ------------------------------------------------
    def check(self, sql: str, schema_ddl: str, dialect: str) -> Verdict:
        if dialect != "sqlite":
            return Verdict.failed(
                "binder",
                f"EphemeralSqliteValidator cannot execute {dialect}; use SqlglotValidator.",
            )
        try:
            assert_read_only_select(sql, dialect)
        except SafetyViolation as exc:
            return Verdict.failed("safety_gate", exc.message, code=exc.code)

        try:
            connection = self._build(schema_ddl)
        except SafetyViolation as exc:
            # The *request's* DDL is bad. Not the model's fault and not repairable
            # by re-prompting, but reported through the same channel.
            return Verdict.failed("binder", f"The supplied schema DDL is unusable: {exc.message}")
        except sqlite3.Error as exc:
            return Verdict.failed("binder", f"The supplied schema DDL is unusable: {exc}")

        try:
            return self._bind_and_run(connection, sql)
        finally:
            connection.close()

    def _bind_and_run(self, connection: sqlite3.Connection, sql: str) -> Verdict:
        try:
            connection.execute(f"EXPLAIN {sql}")
        except sqlite3.Error as exc:
            return Verdict.failed("binder", str(exc), stage="bind")
        if not self.execute:
            return Verdict.passed(stage="bind")
        try:
            rows = self._run_capped(connection, sql)
        except TimeoutError:
            return Verdict.failed(
                "binder",
                f"The query did not finish within {self.timeout_s:g}s against an empty "
                "sample database; simplify it.",
                stage="execute",
            )
        except sqlite3.Error as exc:
            return Verdict.failed("binder", str(exc), stage="execute")
        return Verdict.passed(stage="execute", rows_sampled=rows)

    # -- ports.SchemaValidator -----------------------------------------------
    def check_ddl(self, ddl: str, dialect: str) -> Verdict:
        if dialect != "sqlite":
            return Verdict.failed(
                "binder",
                f"EphemeralSqliteValidator cannot execute {dialect}; use SqlglotValidator.",
            )
        try:
            statements = assert_ddl_only(ddl, dialect)
        except SafetyViolation as exc:
            return Verdict.failed("safety_gate", exc.message, code=exc.code)
        connection = sqlite3.connect(":memory:")
        try:
            for statement in statements:
                try:
                    connection.execute(statement.sql(dialect="sqlite"))
                except sqlite3.Error as exc:
                    return Verdict.failed(
                        "binder", f"{exc} (in: {statement.sql(dialect='sqlite')[:120]})"
                    )
            return Verdict.passed(tables=len(statements))
        finally:
            connection.close()

    # -- internals -----------------------------------------------------------
    def _build(self, schema_ddl: str) -> sqlite3.Connection:
        statements = assert_ddl_only(schema_ddl, "sqlite")
        connection = sqlite3.connect(":memory:")
        try:
            for statement in statements:
                connection.execute(statement.sql(dialect="sqlite"))
            connection.commit()
            # Read-only from here on, regardless of what the candidate contains.
            connection.execute("PRAGMA query_only=ON")
        except BaseException:
            connection.close()
            raise
        return connection

    def _run_capped(self, connection: sqlite3.Connection, sql: str) -> int:
        deadline = time.monotonic() + self.timeout_s
        timed_out = False

        def _watchdog() -> int:
            nonlocal timed_out
            if time.monotonic() > deadline:
                timed_out = True
                return 1  # non-zero aborts the statement
            return 0

        connection.set_progress_handler(_watchdog, 1000)
        try:
            cursor = connection.execute(sql)
            rows = cursor.fetchmany(self.row_cap)
            cursor.close()
        except sqlite3.Error:
            if timed_out:
                raise TimeoutError from None
            raise
        finally:
            connection.set_progress_handler(None, 0)
        if timed_out:
            raise TimeoutError
        return len(rows)
