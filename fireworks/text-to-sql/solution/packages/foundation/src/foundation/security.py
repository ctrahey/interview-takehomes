"""The SQL safety gate for `foundation.sample_db.query()` (D9).

D9: "Query path: single statement only; `SELECT`/`WITH` allowlist enforced on
the `sqlglot` AST before execution, never by string matching." This module is
that gate. It is enforced by `foundation` itself, independent of whatever
validation `t2s_core` may have already done -- a caller here is never
trusted to have sanitized its own input.

Two layers, both AST-based (no substring/regex matching on the raw SQL
text anywhere in this module):

1. **Exactly one statement.** `sqlglot.parse` splits on `;`; anything other
   than exactly one non-empty parsed statement is rejected. This is what
   stops SQL-injection-by-stacking (`SELECT 1; DROP TABLE x;`).
2. **Statement-kind allowlist.** The single statement's *top-level* AST node
   must be one of `SELECT`, `UNION`, `INTERSECT`, `EXCEPT` (a `WITH ... SELECT
   ...` CTE parses as a `Select` node carrying a `with` argument, not as a
   separate top-level kind, so allowing `Select` covers `WITH` too). Anything
   else -- `INSERT`, `UPDATE`, `DELETE`, `CREATE`, `DROP`, `ALTER`, `PRAGMA`,
   `ATTACH`, `DETACH`, `VACUUM`, `REINDEX`, or a dialect construct sqlglot
   can't classify at all (falls back to a generic `Command` node) -- is
   rejected.

   As defense in depth against constructs this module hasn't anticipated,
   the *entire* AST (not just the top-level node) is also walked for any
   node whose type name matches a small denylist of mutating/administrative
   statement kinds, in case such a thing could ever appear nested (e.g.
   inside a subquery). This is belt-and-suspenders: no known SQLite grammar
   admits a DML/DDL statement as a sub-expression of a `SELECT`, but the
   check costs nothing and fails closed if that assumption is ever wrong.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

_ALLOWED_TOP_LEVEL: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.Union,
    exp.Intersect,
    exp.Except,
)

_DENYLIST_ANYWHERE: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Pragma,
    exp.Attach,
    exp.Detach,
    exp.Command,
    exp.TruncateTable,
    exp.Merge,
)


class UnsafeQueryError(ValueError):
    """Raised when a query fails the D9 safety gate."""


def assert_safe_select(sql: str, dialect: str) -> exp.Expression:
    """Validate `sql` against the D9 safety gate; return the parsed AST if it passes.

    Raises `UnsafeQueryError` (never a bare parser exception) describing
    which rule was violated.
    """
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except ParseError as exc:
        raise UnsafeQueryError(f"query does not parse under dialect {dialect!r}: {exc}") from exc

    if len(statements) == 0:
        raise UnsafeQueryError("empty query")
    if len(statements) > 1:
        raise UnsafeQueryError(f"single statement only; found {len(statements)} statements")

    stmt = statements[0]
    if not isinstance(stmt, _ALLOWED_TOP_LEVEL):
        raise UnsafeQueryError(
            f"statement kind {type(stmt).__name__!r} is not allowed; "
            "only SELECT / WITH (and set operations over them) may run"
        )

    for node in stmt.walk():
        if isinstance(node, _DENYLIST_ANYWHERE):
            raise UnsafeQueryError(f"disallowed construct {type(node).__name__!r} found in query")

    return stmt
