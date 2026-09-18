"""D9 safety gate.

Generated SQL is untrusted input to our own execution path, so it is checked on
the **sqlglot AST**, never by string matching. String matching loses to
``SELECT 1; DROP TABLE t``, to comments, to case, and -- the case that actually
motivated this -- to ``WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x``,
which sqlglot parses as a top-level ``Select`` with an ``Insert`` buried in a CTE.
Hence the deep scan: a forbidden node *anywhere* in the tree rejects the
statement, not just at the root.
"""

from __future__ import annotations

from typing import cast

from sqlglot import exp, parse
from sqlglot.errors import ParseError

from t2s_core.errors import SafetyViolation

__all__ = [
    "FORBIDDEN_FUNCTIONS",
    "FORBIDDEN_NODES",
    "assert_ddl_only",
    "assert_read_only_select",
    "sqlglot_dialect",
]

#: Allowed at the root of a query. ``WITH ... SELECT`` parses as ``Select``;
#: set operations parse as ``SetOperation`` subclasses.
_ALLOWED_ROOTS: tuple[type[exp.Expression], ...] = (exp.Select, exp.SetOperation, exp.Subquery)

#: Rejected anywhere in the tree. ``exp.Command`` is sqlglot's fallback for
#: syntax it does not model (VACUUM, EXPLAIN, ...) -- unmodelled means unchecked,
#: so it is refused on principle.
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Attach,
    exp.Detach,
    exp.Pragma,
    exp.Command,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Set,
    exp.Copy,
    exp.Use,
    exp.Grant,
)

#: SQLite functions that reach outside the database.
FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset(
    {
        "load_extension",
        "readfile",
        "writefile",
        "edit",
        "fts3_tokenizer",
        "sqlite_compileoption_get",
    }
)

_DDL_ALLOWED_ROOTS: tuple[type[exp.Expression], ...] = (exp.Create,)
_DDL_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    node for node in FORBIDDEN_NODES if node is not exp.Create
)


def _parse_statements(sql: str, dialect: str) -> list[exp.Expression]:
    if not sql or not sql.strip():
        raise SafetyViolation("empty_statement", "The statement is empty.")
    try:
        parsed = parse(sql, dialect=sqlglot_dialect(dialect))
    except ParseError as exc:
        raise SafetyViolation("unparseable", f"SQL does not parse as {dialect}: {exc}") from exc
    statements = cast("list[exp.Expression]", [s for s in parsed if s is not None])
    if not statements:
        raise SafetyViolation("empty_statement", "The statement is empty.")
    if len(statements) > 1:
        raise SafetyViolation(
            "multiple_statements",
            f"Exactly one statement is allowed; {len(statements)} were supplied. "
            "Statement batching is refused.",
        )
    return statements


def sqlglot_dialect(dialect: str) -> str:
    return {"postgres": "postgres", "mysql": "mysql", "sqlite": "sqlite"}.get(dialect, dialect)


def _scan(tree: exp.Expression, forbidden: tuple[type[exp.Expression], ...]) -> None:
    for node in tree.walk():
        if isinstance(node, forbidden):
            kind = type(node).__name__.upper()
            raise SafetyViolation(
                "forbidden_statement",
                f"{kind} is not permitted on this path; only read-only queries are executed.",
            )
        if isinstance(node, exp.Anonymous) and node.name.lower() in FORBIDDEN_FUNCTIONS:
            raise SafetyViolation(
                "forbidden_function",
                f"The function {node.name}() is not permitted; it reaches outside the database.",
            )


def assert_read_only_select(sql: str, dialect: str = "sqlite") -> exp.Expression:
    """Return the parsed AST, or raise :class:`SafetyViolation`.

    Enforces: parseable, exactly one statement, root is SELECT/WITH/set-operation,
    and no forbidden node or function anywhere in the tree.
    """
    statement = _parse_statements(sql, dialect)[0]
    if not isinstance(statement, _ALLOWED_ROOTS):
        raise SafetyViolation(
            "forbidden_statement",
            f"{type(statement).__name__.upper()} is not permitted; the query path accepts only "
            "a single SELECT (optionally with CTEs or set operations).",
        )
    _scan(statement, FORBIDDEN_NODES)
    return statement


def assert_ddl_only(ddl: str, dialect: str = "sqlite") -> list[exp.Expression]:
    """The mirror gate for the schema path: CREATE statements only, no DML, no
    DROP/ALTER/ATTACH/PRAGMA. Multiple statements are expected here."""
    if not ddl or not ddl.strip():
        raise SafetyViolation("empty_statement", "The DDL script is empty.")
    try:
        parsed = parse(ddl, dialect=sqlglot_dialect(dialect))
    except ParseError as exc:
        raise SafetyViolation("unparseable", f"DDL does not parse as {dialect}: {exc}") from exc
    statements = cast("list[exp.Expression]", [s for s in parsed if s is not None])
    if not statements:
        raise SafetyViolation("empty_statement", "The DDL script is empty.")
    for statement in statements:
        if not isinstance(statement, _DDL_ALLOWED_ROOTS):
            raise SafetyViolation(
                "forbidden_statement",
                f"{type(statement).__name__.upper()} is not permitted in a schema; emit only "
                "CREATE TABLE / CREATE INDEX / CREATE VIEW statements.",
            )
        _scan(statement, _DDL_FORBIDDEN_NODES)
    return statements
