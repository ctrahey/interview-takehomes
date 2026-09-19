"""Sample database lifecycle: create -> load -> query -> clear -> destroy (design §4, D9).

Files live under `foundation.paths.managed_directory()`, named only by the
database's UUID (`foundation.paths.database_path`) -- a client-supplied path
never reaches the filesystem.

`query()` layers three independent enforcement mechanisms, so no single
bypassed check compromises the guarantee:

1. `foundation.security.assert_safe_select` -- an AST-level allowlist gate
   (single statement, `SELECT`/`WITH` only) run *before* the query ever
   touches sqlite.
2. The connection itself is opened read-only at the SQLite driver level
   (`file:...?mode=ro`), and `PRAGMA query_only=ON` is set for good measure
   -- so even a hypothetical gap in (1) still can't write.
3. A wall-clock timeout (via `sqlite3`'s progress handler, which SQLite
   calls periodically during query execution) and a row cap (via
   `fetchmany(cap + 1)`, truncating rather than raising) bound the resource
   cost of a query that *does* pass the gate.
"""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from foundation import ddl, paths, security

DEFAULT_QUERY_TIMEOUT_SECONDS = 5.0
DEFAULT_ROW_CAP = 1000

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SampleDatabaseError(RuntimeError):
    """Base class for sample-database lifecycle errors."""


class DatabaseNotFoundError(SampleDatabaseError):
    pass


class QueryTimeoutError(SampleDatabaseError):
    pass


class InvalidIdentifierError(SampleDatabaseError):
    pass


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    row_count: int
    truncated: bool


def _existing_path(database_id: uuid.UUID) -> Path:
    path = paths.database_path(database_id)
    if not path.exists():
        raise DatabaseNotFoundError(f"no sample database for id {database_id}")
    return path


def _validate_identifier(name: str) -> str:
    if not _IDENTIFIER_RE.match(name):
        raise InvalidIdentifierError(f"not a valid SQL identifier: {name!r}")
    return name


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def create(schema_ddl: str, *, dialect: str = "sqlite") -> uuid.UUID:
    """Create a new sample database file and execute `schema_ddl` against it.

    Returns the new database's id. `schema_ddl` is expected to already be
    concrete DDL (e.g. from `foundation.ddl.render_ddl`) -- creating tables
    is an administrative operation, not subject to the `query()` read-only
    gate.
    """
    if dialect not in ddl.EXECUTABLE_DIALECTS:
        raise ValueError(
            f"cannot create a live sample database for dialect {dialect!r}; "
            f"only {ddl.EXECUTABLE_DIALECTS} are executable (D5)"
        )
    database_id = uuid.uuid4()
    path = paths.database_path(database_id)
    if path.exists():
        raise SampleDatabaseError(f"database file already exists for id {database_id}")

    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(schema_ddl)
        conn.commit()
    except sqlite3.Error as exc:
        conn.close()
        path.unlink(missing_ok=True)
        raise SampleDatabaseError(f"failed to apply schema DDL: {exc}") from exc
    finally:
        conn.close()
    return database_id


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------


def load(database_id: uuid.UUID, dataset_rows: dict[str, list[dict[str, object]]]) -> int:
    """Insert rows into an existing sample database. Returns the total row count inserted.

    `dataset_rows` maps table name -> list of {column: value} dicts (the
    shape stored in `foundation.models.Dataset.rows`). Table and column
    names are validated as plain SQL identifiers (defense in depth -- they
    should already come from a known schema, never raw user text) and always
    passed through parameterized `?` placeholders for values.
    """
    path = _existing_path(database_id)
    conn = sqlite3.connect(str(path))
    inserted = 0
    try:
        cursor = conn.cursor()
        for table, rows in dataset_rows.items():
            _validate_identifier(table)
            for row in rows:
                columns = list(row.keys())
                for column in columns:
                    _validate_identifier(column)
                col_list = ", ".join(f'"{c}"' for c in columns)
                placeholders = ", ".join("?" for _ in columns)
                cursor.execute(
                    f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})',
                    [row[c] for c in columns],
                )
                inserted += 1
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise SampleDatabaseError(f"failed to load dataset: {exc}") from exc
    finally:
        conn.close()
    return inserted


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------


def query(
    database_id: uuid.UUID,
    sql: str,
    *,
    dialect: str = "sqlite",
    timeout_seconds: float = DEFAULT_QUERY_TIMEOUT_SECONDS,
    row_cap: int = DEFAULT_ROW_CAP,
    allow_catalog: bool = False,
) -> QueryResult:
    """Run a read-only query against a sample database, under D9's guarantees.

    Raises `foundation.security.UnsafeQueryError` for anything that isn't a
    single `SELECT`/`WITH` statement, `QueryTimeoutError` if it runs longer
    than `timeout_seconds` of wall-clock time, and truncates (rather than
    erroring on) result sets larger than `row_cap`, reporting
    `QueryResult.truncated`.

    D12: references to `sqlite_master`/`sqlite_schema` raise
    `foundation.security.CatalogAccessDeniedError` (a subclass of
    `UnsafeQueryError`) unless `allow_catalog=True` is passed explicitly.
    Catalog enumeration against a persisted, potentially shared sample
    database is reconnaissance, not a question the user's own schema
    answers -- "what tables exist" has a deterministic answer via the CRUD
    endpoints instead.
    """
    security.assert_safe_select(sql, dialect, allow_catalog=allow_catalog)
    path = _existing_path(database_id)

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        start = time.monotonic()

        def _abort_if_timed_out() -> int:
            return 1 if (time.monotonic() - start) > timeout_seconds else 0

        conn.set_progress_handler(_abort_if_timed_out, 1000)
        cursor = conn.cursor()
        try:
            cursor.execute(sql)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            fetched = cursor.fetchmany(row_cap + 1)
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                raise QueryTimeoutError(
                    f"query exceeded {timeout_seconds}s wall-clock timeout"
                ) from exc
            raise SampleDatabaseError(str(exc)) from exc
        finally:
            conn.set_progress_handler(None, 0)

        truncated = len(fetched) > row_cap
        rows = fetched[:row_cap]
        return QueryResult(columns=columns, rows=rows, row_count=len(rows), truncated=truncated)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# row counts and clear
# ---------------------------------------------------------------------------


def row_counts(database_id: uuid.UUID, tables: Sequence[str]) -> dict[str, int]:
    """How many rows each named table holds. Opened read-only.

    W17: the confirmation prompt for a destructive action has to say *precisely*
    what is about to be lost, and "3 tables" is not that -- "142 rows across 3
    tables" is. Table names come from the caller's own stored entity graph and
    are validated as identifiers anyway (defense in depth); no name from a model
    or a user ever reaches this.

    A table named in `tables` that does not exist in the file is reported as 0
    rather than raising: a schema version can legitimately name a table a
    database predates, and refusing to describe a database because one table is
    missing would block the very confirmation that protects it.
    """
    path = _existing_path(database_id)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    counts: dict[str, int] = {}
    try:
        conn.execute("PRAGMA query_only=ON")
        for table in tables:
            _validate_identifier(table)
            try:
                row = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
            except sqlite3.Error:
                counts[table] = 0
            else:
                counts[table] = int(row[0]) if row else 0
    finally:
        conn.close()
    return counts


def clear(database_id: uuid.UUID, tables: Sequence[str]) -> int:
    """Delete every row from the named tables, keeping the instance and its DDL.

    The other half of W17's lifecycle pair: `destroy` removes the instance,
    `clear` empties it. Deliberately a distinct operation rather than
    "destroy then create", because the two have different consequences -- the
    database id survives a clear, so anything pointing at it still resolves.

    Foreign keys are disabled for the duration and the whole thing is one
    transaction: emptying a graph of tables in dependency order is a topological
    sort we would have to get right on every schema, and a half-cleared database
    is worse than either outcome. This is an administrative operation on a file
    we own, like `create` and `load`, and it deliberately does not route through
    `query()`'s read-only gate (D9) -- that gate exists for *generated* SQL, and
    nothing generated reaches here.
    """
    path = _existing_path(database_id)
    names = [_validate_identifier(t) for t in tables]
    conn = sqlite3.connect(str(path))
    deleted = 0
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        cursor = conn.cursor()
        for table in names:
            try:
                cursor.execute(f'DELETE FROM "{table}"')
            except sqlite3.Error:
                continue  # a table this schema names and this file does not have
            deleted += cursor.rowcount if cursor.rowcount > 0 else 0
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise SampleDatabaseError(f"failed to clear sample database: {exc}") from exc
    finally:
        conn.close()
    return deleted


# ---------------------------------------------------------------------------
# destroy
# ---------------------------------------------------------------------------


def destroy(database_id: uuid.UUID) -> None:
    """Remove a sample database's file. Idempotent (missing file is not an error)."""
    path = paths.database_path(database_id)
    path.unlink(missing_ok=True)


def exists(database_id: uuid.UUID) -> bool:
    return paths.database_path(database_id).exists()
