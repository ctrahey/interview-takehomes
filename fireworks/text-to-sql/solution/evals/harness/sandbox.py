"""Seeded fixtures + the sandbox that executes gold and candidate SQL.

Candidate SQL is model output, so it is untrusted input to *our* execution path
(D9). Every execution here goes through the same three independent mechanisms
that guard ``foundation.sample_db.query``:

1. ``foundation.security.assert_safe_select`` — the AST allowlist gate. Reused
   deliberately rather than reimplemented: the scorer's ``forbid_ddl_dml`` check
   and the engine's execution gate must not be allowed to drift apart. It is
   called with ``allow_catalog=True``, and that is a deliberate, narrow opt-in:
   D12 scopes the catalog denial to *persisted, potentially shared* sample
   databases and states that in ``t2s_core`` — where the request's own DDL is
   the entire world — catalog access is correct and stays allowed. These
   fixtures are throwaway files rebuilt from the corpus DDL on every run, which
   is exactly that situation. Catalog use is still *recorded* per item, so if a
   model answers a question by reading ``sqlite_master`` the report says so.
2. A read-only connection (``file:...?mode=ro`` plus ``PRAGMA query_only=ON``).
3. A wall-clock timeout (sqlite3 progress handler) and a row cap.

The fixtures themselves are throwaway files under a temp directory, rebuilt from
``ddl.sql`` + ``seed.sql`` on every run, so nothing a candidate query does can
persist — and the harness never touches ``foundation.paths``' managed directory.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from evals.harness.corpus import Corpus

from foundation.security import UnsafeQueryError, assert_safe_select

__all__ = ["ExecutionError", "ExecutionResult", "Fixtures", "fixtures"]

DEFAULT_TIMEOUT_S = 10.0
#: Generous relative to the fixtures (largest table is ~260 rows) so that a cap
#: hit means "the candidate asked for something pathological", not "our cap is
#: too tight". A truncated result is reported, never silently compared.
DEFAULT_ROW_CAP = 5000
_PROGRESS_INSTRUCTIONS = 1000


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    truncated: bool
    elapsed_ms: int


class ExecutionError(RuntimeError):
    """Any reason a statement did not produce a comparable result set."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        #: one of: ``safety_gate``, ``timeout``, ``engine``, ``truncated``
        self.kind = kind
        self.message = message


@dataclass(slots=True)
class Fixtures:
    """Seeded SQLite databases, one per corpus schema, in a temp directory."""

    directory: Path
    paths: dict[str, Path] = field(default_factory=dict)

    def build(self, corpus: Corpus) -> None:
        for name, schema in corpus.schemas.items():
            path = self.directory / f"{name}.sqlite3"
            connection = sqlite3.connect(str(path))
            try:
                connection.executescript(schema.ddl)
                connection.executescript(schema.seed)
                connection.commit()
            finally:
                connection.close()
            self.paths[name] = path

    def execute(
        self,
        schema: str,
        sql: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        row_cap: int = DEFAULT_ROW_CAP,
    ) -> ExecutionResult:
        """Run one read-only statement against a seeded fixture.

        Raises :class:`ExecutionError` for every non-success outcome, including a
        row-cap hit — the scorer must never compare a truncated result set to a
        complete one and call the difference an accuracy signal.
        """
        try:
            assert_safe_select(sql, "sqlite", allow_catalog=True)
        except UnsafeQueryError as exc:
            raise ExecutionError("safety_gate", str(exc)) from exc

        path = self.paths[schema]
        started = time.perf_counter()
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            deadline = time.monotonic() + timeout_s
            connection.set_progress_handler(
                lambda: 1 if time.monotonic() > deadline else 0, _PROGRESS_INSTRUCTIONS
            )
            try:
                cursor = connection.execute(sql)
                columns = tuple(d[0] for d in cursor.description or ())
                fetched = cursor.fetchmany(row_cap + 1)
                cursor.close()
            except sqlite3.Error as exc:
                kind = "timeout" if "interrupt" in str(exc).lower() else "engine"
                raise ExecutionError(kind, str(exc)) from exc
            finally:
                connection.set_progress_handler(None, 0)
        finally:
            connection.close()

        if len(fetched) > row_cap:
            raise ExecutionError(
                "truncated",
                f"result exceeded the {row_cap}-row cap; not comparable",
            )
        return ExecutionResult(
            columns=columns,
            rows=tuple(tuple(row) for row in fetched),
            truncated=False,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


@contextmanager
def fixtures(corpus: Corpus) -> Iterator[Fixtures]:
    """Build every schema's seeded database; remove them all on exit."""
    with TemporaryDirectory(prefix="t2s-eval-") as tmp:
        built = Fixtures(directory=Path(tmp))
        built.build(corpus)
        yield built
