"""Entity graph <-> DDL, via sqlglot (D6).

``render_ddl(graph, dialect)`` is a pure function: `EntityGraph` in, DDL text
out, same input always produces the same output. It works by constructing a
sqlglot AST (`exp.Create` / `exp.Schema` / `exp.ColumnDef` / constraint nodes)
directly from the graph and asking sqlglot's generator for the dialect's SQL
text -- there is no string templating anywhere in this module.

``parse_ddl(ddl, dialect)`` is the inverse: DDL text in, `EntityGraph` out.
It is intentionally partial. ``CREATE TABLE`` statements contribute to the
resulting graph directly. ``ALTER TABLE ... ADD [CONSTRAINT ...] UNIQUE (...)``
and ``ALTER TABLE ... ADD [CONSTRAINT ...] FOREIGN KEY (...) REFERENCES ...``
are folded into the *same* graph table as if the constraint had been declared
inline in the `CREATE TABLE` body (see "ALTER-expressed constraints" below) --
this is what lets a Postgres schema that leans on ``ALTER TABLE`` (common for
constraints added after the fact, or for a pair of tables with a circular FK)
still arrive as one dialect-neutral graph. Every other statement
(``CREATE INDEX``, ``CREATE VIEW``, unsupported ``ALTER TABLE`` actions,
``COMMENT ON``, ...) is reported as a `ParseResult.warning` and skipped, never
silently dropped. Within a `CREATE TABLE`, constructs the graph has no field
for are *also* reported as warnings rather than causing a hard failure or
vanishing quietly. See "Known inversion limits" below.

## ALTER-expressed constraints (D6)

The graph does not care whether a `UNIQUE` or `FOREIGN KEY` constraint arrived
via a `CREATE TABLE` column/table-level clause or a later `ALTER TABLE ... ADD
...` statement -- both become the same `UniqueConstraint` / `ForeignKey` graph
entry on the target table. Concretely, `parse_ddl` makes two passes over the
parsed statement list:

1. Every `CREATE TABLE` is parsed into a `_TableAccumulator` (kept, not
   discarded, so it can still be mutated in pass 2), in declaration order.
2. Every `ALTER TABLE ... ADD CONSTRAINT? (UNIQUE (...) | FOREIGN KEY (...)
   REFERENCES ...)` is folded into the accumulator for its target table via
   the *same* `add_unique` / `add_foreign_key` methods pass 1 uses for inline
   constraints -- one code path, two syntactic origins. This is also where
   deduplication happens: a constraint that is structurally identical
   (same columns, same reference) to one already recorded for that table is
   silently folded rather than appended twice, so a source file that declares
   the same `ALTER TABLE ... ADD UNIQUE (a, b)` twice (legal, if redundant,
   PostgreSQL) does not produce two identical `UniqueConstraint` entries or
   fail to render as SQLite (which rejects a duplicate index outright). Only
   the two `ADD ... UNIQUE` / `ADD ... FOREIGN KEY` action shapes are folded
   this way; any other `ALTER TABLE` action (`ADD COLUMN`, `DROP ...`,
   `RENAME ...`, an unsupported constraint shape, or an `ALTER TABLE`
   targeting a table this parse never saw a `CREATE TABLE` for) is reported as
   a warning and skipped -- never silently dropped, never a hard failure.

Because pass 2 runs after *all* of pass 1, this also resolves a genuinely
circular pair of foreign keys expressed across two `ALTER TABLE` statements
(table A's FK to B, and B's FK to A) -- by the time either `ALTER TABLE` is
applied, both tables already exist as accumulators, so which one was
declared/altered "first" in the source text is irrelevant.

Dialect support: SQLite (D5's execution target) and Postgres (D5's
generation-only target). Both directions raise `ValueError` for any other
dialect string -- this module does not claim support it has not verified.

## Known inversion limits (parse_ddl)

These are honest gaps, not oversights:

- **Only `CREATE TABLE` is captured.** Indexes, views, triggers, and any DDL
  outside a `CREATE TABLE` body are skipped with a warning.
- **Column/table comments do not round-trip.** `COMMENT ON ...` (Postgres)
  and inline `COMMENT '...'` (MySQL) are different statement/clause shapes
  per dialect; SQLite has no DDL-level comment construct at all. Comments are
  a graph field for hand-authored models, but `parse_ddl` does not attempt to
  recover them, and `render_ddl` does not emit them.
- **Inline generated/computed columns, `COLLATE`, `DEFERRABLE`, and
  identity/`AUTOINCREMENT` column constraints are not modeled.** They parse
  without error (sqlglot handles them) but are silently absent from the
  resulting `Column` -- reported as a warning naming the column.
- **CHECK and DEFAULT expressions are captured as generic (dialect-less) SQL
  text**, via ``expr.sql()`` with no `dialect=` argument. Re-rendering that
  text against a *different* dialect than it was authored in re-parses it
  generically and asks the target dialect's generator for its text; this is
  correct for portable expressions (`total >= 0`, `CURRENT_TIMESTAMP`) but
  dialect-specific functions inside such expressions (e.g. Postgres `now()`
  vs SQLite `CURRENT_TIMESTAMP`, or `::` casts) may not transpile perfectly.
  This is a sqlglot transpilation limit, not something this module works
  around.
- **`EXCLUDE`, `EXCLUDE CONSTRAINT`, table inheritance, partitioning, and
  storage options** (e.g. `ENGINE=InnoDB`) are not modeled; if present they
  are reported as an "unsupported table-level construct" warning.

A parse that produces only warnings and no tables (e.g. a DDL script that is
entirely `CREATE INDEX` statements) is not an error -- `ParseResult.graph`
will simply have an empty `tables` list. Callers that need "the DDL was fully
captured" as a hard requirement should check `ParseResult.warnings == []`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

import sqlglot
from sqlglot import exp

from foundation.graph import (
    CheckConstraint,
    Column,
    EntityGraph,
    ForeignKey,
    Table,
    UniqueConstraint,
)

SUPPORTED_DIALECTS = ("sqlite", "postgres")

# D5: SQLite is the only engine we ever execute against. Postgres DDL
# rendered here is a generation target only, never run by this codebase.
EXECUTABLE_DIALECTS = ("sqlite",)


def _check_dialect(dialect: str) -> None:
    if dialect not in SUPPORTED_DIALECTS:
        raise ValueError(f"unsupported dialect: {dialect!r} (supported: {SUPPORTED_DIALECTS})")


# ---------------------------------------------------------------------------
# graph -> DDL
# ---------------------------------------------------------------------------


def render_ddl(graph: EntityGraph, dialect: str) -> str:
    """Render a dialect-neutral entity graph to concrete, semicolon-terminated DDL text.

    Table order in the output is a dependency-respecting topological sort of
    FK references (self-references and cycles fall back to declaration order
    for the tables involved in the cycle -- harmless: SQLite does not check
    FK target existence at `CREATE TABLE` time unless
    `PRAGMA foreign_keys=ON`, and Postgres DDL from this module is never
    executed, only generated, per D5).
    """
    _check_dialect(dialect)
    statements = [_render_table(t, dialect) for t in _topo_sorted(graph.tables)]
    return ";\n\n".join(statements) + (";\n" if statements else "")


def _topo_sorted(tables: list[Table]) -> list[Table]:
    by_name = {t.name: t for t in tables}
    visited: set[str] = set()
    active: set[str] = set()
    order: list[Table] = []

    def visit(t: Table) -> None:
        if t.name in visited or t.name in active:
            return
        active.add(t.name)
        for fk in t.foreign_keys:
            ref = by_name.get(fk.ref_table)
            if ref is not None and ref.name != t.name:
                visit(ref)
        active.discard(t.name)
        visited.add(t.name)
        order.append(t)

    for t in tables:
        visit(t)
    return order


def _render_table(table: Table, dialect: str) -> str:
    expressions: list[exp.Expression] = [_render_column(c, table) for c in table.columns]

    # A single-column, unnamed PK is attached inline on the column itself
    # (lets SQLite recognize `INTEGER PRIMARY KEY` as its rowid alias).
    # Anything else (composite, or explicitly named) becomes a table-level
    # constraint.
    if len(table.primary_key) > 1 or (table.primary_key and table.primary_key_name):
        pk_node = exp.PrimaryKey(expressions=[exp.to_identifier(c) for c in table.primary_key])
        expressions.append(_maybe_named(pk_node, table.primary_key_name))

    for fk in table.foreign_keys:
        expressions.append(_render_fk(fk))
    for chk in table.checks:
        expressions.append(_render_check(chk))
    for uq in table.unique_constraints:
        expressions.append(_render_unique(uq))

    schema = exp.Schema(this=exp.to_table(table.name), expressions=expressions)
    create = exp.Create(this=schema, kind="TABLE")
    return create.sql(dialect=dialect, pretty=False)


def _is_solo_inline_pk(col: Column, table: Table) -> bool:
    return (
        len(table.primary_key) == 1
        and table.primary_key[0] == col.name
        and not table.primary_key_name
    )


def _render_column(col: Column, table: Table) -> exp.ColumnDef:
    constraints: list[exp.ColumnConstraint] = []
    solo_pk = _is_solo_inline_pk(col, table)
    if solo_pk:
        constraints.append(exp.ColumnConstraint(kind=exp.PrimaryKeyColumnConstraint()))
    if not col.nullable and not solo_pk:
        constraints.append(exp.ColumnConstraint(kind=exp.NotNullColumnConstraint()))
    if col.unique:
        constraints.append(exp.ColumnConstraint(kind=exp.UniqueColumnConstraint()))
    if col.default is not None:
        constraints.append(
            exp.ColumnConstraint(kind=exp.DefaultColumnConstraint(this=_parse_expr(col.default)))
        )
    return exp.ColumnDef(
        this=exp.to_identifier(col.name),
        kind=exp.DataType.build(col.type),
        constraints=constraints or None,
    )


def _render_fk(fk: ForeignKey) -> exp.Expression:
    options: list[str] = []
    if fk.on_delete:
        options.append(f"ON DELETE {fk.on_delete}")
    if fk.on_update:
        options.append(f"ON UPDATE {fk.on_update}")
    reference = exp.Reference(
        this=exp.Schema(
            this=exp.to_table(fk.ref_table),
            expressions=[exp.to_identifier(c) for c in fk.ref_columns],
        ),
        options=options or None,
    )
    node = exp.ForeignKey(
        expressions=[exp.to_identifier(c) for c in fk.columns], reference=reference
    )
    return _maybe_named(node, fk.name)


def _render_check(chk: CheckConstraint) -> exp.Expression:
    node = exp.Check(this=_parse_expr(chk.expression))
    return _maybe_named(node, chk.name)


def _render_unique(uq: UniqueConstraint) -> exp.Expression:
    node = exp.UniqueColumnConstraint(
        this=exp.Schema(expressions=[exp.to_identifier(c) for c in uq.columns])
    )
    return _maybe_named(node, uq.name)


def _maybe_named(node: exp.Expression, name: str | None) -> exp.Expression:
    if name is None:
        return node
    return exp.Constraint(this=exp.to_identifier(name), expressions=[node])


def _parse_expr(text: str) -> exp.Expression:
    # sqlglot's own type hints for `parse_one` return the more general `exp.Expr`
    # base; in practice it always returns a concrete `exp.Expression` node for
    # real SQL text, so narrow it back here rather than loosening this
    # module's types to match a wider-than-necessary upstream signature.
    return cast("exp.Expression", sqlglot.parse_one(text))


# ---------------------------------------------------------------------------
# DDL -> graph
# ---------------------------------------------------------------------------


@dataclass
class ParseResult:
    graph: EntityGraph
    warnings: list[str] = field(default_factory=list)


def parse_ddl(ddl: str, dialect: str) -> ParseResult:
    """Parse DDL text into a dialect-neutral entity graph. The partial inverse of `render_ddl`.

    Two passes (see the module docstring's "ALTER-expressed constraints"
    section): every `CREATE TABLE` first, then every `ALTER TABLE ... ADD
    UNIQUE|FOREIGN KEY` folded into the table it targets. This order is what
    lets a circular pair of FKs expressed as two separate `ALTER TABLE`
    statements resolve regardless of declaration order.

    See the module docstring's "Known inversion limits" section for exactly
    what does and does not survive.
    """
    _check_dialect(dialect)

    warnings: list[str] = []
    accumulators: dict[str, _TableAccumulator] = {}
    order: list[str] = []
    deferred_alters: list[exp.Alter] = []

    for stmt in sqlglot.parse(ddl, read=dialect):
        if stmt is None:
            continue
        if isinstance(stmt, exp.Create) and str(stmt.args.get("kind", "")).upper() == "TABLE":
            acc, table_warnings = _parse_table(stmt, dialect)
            if acc.table_name in accumulators:
                warnings.append(f"duplicate CREATE TABLE for {acc.table_name!r}; keeping the last")
            else:
                order.append(acc.table_name)
            accumulators[acc.table_name] = acc
            warnings.extend(table_warnings)
        elif isinstance(stmt, exp.Alter) and str(stmt.args.get("kind", "")).upper() == "TABLE":
            deferred_alters.append(stmt)
        else:
            snippet = stmt.sql(dialect=dialect)[:80]
            warnings.append(f"skipped non-CREATE-TABLE statement: {snippet}")

    for alter_stmt in deferred_alters:
        warnings.extend(_apply_alter_table(alter_stmt, accumulators))

    tables = [accumulators[name].to_table() for name in order]
    return ParseResult(graph=EntityGraph(tables=tables), warnings=warnings)


def _apply_alter_table(stmt: exp.Alter, accumulators: dict[str, _TableAccumulator]) -> list[str]:
    """Fold one `ALTER TABLE`'s `ADD UNIQUE` / `ADD FOREIGN KEY` actions into its table.

    Any other action (`ADD COLUMN`, `DROP ...`, an unrecognized constraint
    shape, or a target table this parse never saw a `CREATE TABLE` for) is
    reported as a warning and skipped -- see the module docstring.
    """
    warnings: list[str] = []
    table_name = stmt.this.name if isinstance(stmt.this, exp.Expression) else None
    acc = accumulators.get(table_name) if table_name else None
    if acc is None:
        snippet = stmt.sql()[:80]
        warnings.append(f"ALTER TABLE targets unknown table {table_name!r}: {snippet}")
        return warnings

    for action in stmt.args.get("actions") or []:
        if not isinstance(action, exp.AddConstraint):
            warnings.append(
                f"unsupported ALTER TABLE action on {table_name!r}: {type(action).__name__}"
            )
            continue
        for node in action.expressions:
            name: str | None = None
            inner: exp.Expression = node
            if isinstance(node, exp.Constraint):
                name = node.this.name if node.this else None
                inner = node.expressions[0] if node.expressions else node
            if isinstance(inner, exp.UniqueColumnConstraint):
                before = len(acc.warnings)
                acc.add_unique(inner, name)
                warnings.extend(acc.warnings[before:])
            elif isinstance(inner, exp.ForeignKey):
                before = len(acc.warnings)
                acc.add_foreign_key(inner, name)
                warnings.extend(acc.warnings[before:])
            else:
                warnings.append(
                    f"unsupported ALTER TABLE ADD CONSTRAINT shape on {table_name!r}: "
                    f"{type(inner).__name__}"
                )
    return warnings


def _identifiers(nodes: list[exp.Expression]) -> list[str]:
    return [n.name for n in nodes]


@dataclass
class _TableAccumulator:
    """Mutable scratch space `_parse_table` fills in while walking a CREATE TABLE's expressions."""

    table_name: str
    warnings: list[str] = field(default_factory=list)
    columns: list[Column] = field(default_factory=list)
    pk_columns: list[str] = field(default_factory=list)
    pk_name: str | None = None
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    checks: list[CheckConstraint] = field(default_factory=list)
    uniques: list[UniqueConstraint] = field(default_factory=list)

    def add_column(self, inner: exp.ColumnDef) -> None:
        col, is_pk, inline_fk, col_warnings = _parse_column(inner, self.table_name)
        self.columns.append(col)
        if is_pk:
            self.pk_columns.append(col.name)
        if inline_fk is not None:
            self.foreign_keys.append(inline_fk)
        self.warnings.extend(col_warnings)

    def add_primary_key(self, inner: exp.PrimaryKey, name: str | None) -> None:
        self.pk_columns.extend(_identifiers(inner.expressions))
        self.pk_name = name

    def add_foreign_key(self, inner: exp.ForeignKey, name: str | None) -> None:
        reference = inner.args.get("reference")
        if reference is None:
            self.warnings.append(f"FOREIGN KEY on {self.table_name!r} has no REFERENCES clause")
            return
        fk = _parse_reference(reference, _identifiers(inner.expressions))
        fk.name = name
        key = (tuple(fk.columns), fk.ref_table, tuple(fk.ref_columns))
        if any((tuple(e.columns), e.ref_table, tuple(e.ref_columns)) == key for e in self.foreign_keys):
            # Structurally identical to one already recorded (e.g. a source
            # file that repeats the same `ALTER TABLE ... ADD FOREIGN KEY`) --
            # fold silently rather than duplicate the constraint in the graph.
            return
        self.foreign_keys.append(fk)

    def add_check(self, inner: exp.Check | exp.CheckColumnConstraint, name: str | None) -> None:
        self.checks.append(CheckConstraint(expression=inner.this.sql(), name=name))

    def add_unique(self, inner: exp.UniqueColumnConstraint, name: str | None) -> None:
        cols = _identifiers(inner.this.expressions) if inner.this is not None else []
        if not cols:
            self.warnings.append(f"unhandled UNIQUE constraint shape on table {self.table_name!r}")
            return
        key = (tuple(cols), name)
        if any((tuple(u.columns), u.name) == key for u in self.uniques):
            # Structurally identical to one already recorded (e.g.
            # tic_tac_toe's upstream `ALTER TABLE turns ADD UNIQUE (game_id,
            # position)` appearing twice) -- fold silently. SQLite raises on
            # a genuinely duplicate index; the graph should not reproduce
            # the duplication just because the source text had it.
            return
        self.uniques.append(UniqueConstraint(columns=cols, name=name))

    def add_unsupported(self, inner: exp.Expression) -> None:
        self.warnings.append(
            f"unsupported table-level construct on {self.table_name!r}: {type(inner).__name__}"
        )

    def dispatch(self, inner: exp.Expression, name: str | None) -> None:
        if isinstance(inner, exp.ColumnDef):
            self.add_column(inner)
        elif isinstance(inner, exp.PrimaryKey):
            self.add_primary_key(inner, name)
        elif isinstance(inner, exp.ForeignKey):
            self.add_foreign_key(inner, name)
        elif isinstance(inner, exp.Check | exp.CheckColumnConstraint):
            self.add_check(inner, name)
        elif isinstance(inner, exp.UniqueColumnConstraint):
            self.add_unique(inner, name)
        else:
            self.add_unsupported(inner)

    def to_table(self) -> Table:
        return Table(
            name=self.table_name,
            columns=self.columns,
            primary_key=self.pk_columns,
            primary_key_name=self.pk_name,
            foreign_keys=self.foreign_keys,
            checks=self.checks,
            unique_constraints=self.uniques,
        )


def _parse_table(stmt: exp.Create, dialect: str) -> tuple[_TableAccumulator, list[str]]:
    schema_node = stmt.this
    if isinstance(schema_node, exp.Schema):
        table_name = schema_node.this.name
        expressions = list(schema_node.expressions)
        leading_warnings: list[str] = []
    else:
        # A CREATE TABLE with no column list at all (unusual, e.g.
        # `CREATE TABLE t AS SELECT ...`) -- nothing to capture.
        table_name = schema_node.name
        expressions = []
        leading_warnings = [f"table {table_name!r} has no column definitions to capture"]

    acc = _TableAccumulator(table_name=table_name, warnings=leading_warnings)

    for node in expressions:
        name: str | None = None
        inner: exp.Expression = node
        if isinstance(node, exp.Constraint):
            name = node.this.name if node.this else None
            inner = node.expressions[0] if node.expressions else node
        acc.dispatch(inner, name)

    return acc, acc.warnings


def _parse_reference(ref: exp.Reference, fk_columns: list[str]) -> ForeignKey:
    schema = ref.this
    ref_table = schema.this.name
    ref_columns = _identifiers(schema.expressions)
    on_delete: str | None = None
    on_update: str | None = None
    for opt in ref.args.get("options") or []:
        text = str(opt).upper()
        if text.startswith("ON DELETE "):
            on_delete = text.removeprefix("ON DELETE ")
        elif text.startswith("ON UPDATE "):
            on_update = text.removeprefix("ON UPDATE ")
    return ForeignKey(
        columns=fk_columns,
        ref_table=ref_table,
        ref_columns=ref_columns,
        on_delete=on_delete,
        on_update=on_update,
    )


# Inline column constraint kinds this module intentionally does not model.
# Presence is reported as a warning (see module docstring); the DDL still
# parses without error, only the specific behavior is not captured in the
# graph.
_UNMODELED_INLINE_CONSTRAINTS = (
    "GeneratedAsIdentityColumnConstraint",
    "AutoIncrementColumnConstraint",
    "CollateColumnConstraint",
    "GeneratedAsRowColumnConstraint",
    "ComputedColumnConstraint",
)


def _parse_column(
    node: exp.ColumnDef, table_name: str
) -> tuple[Column, bool, ForeignKey | None, list[str]]:
    nullable = True
    unique = False
    default: str | None = None
    is_pk = False
    inline_fk: ForeignKey | None = None
    warnings: list[str] = []
    col_name = node.this.name
    if node.kind is None:
        raise ValueError(f"column {table_name!r}.{col_name!r} has no declared type")

    for constraint in node.constraints or []:
        kind = constraint.kind
        kind_name = type(kind).__name__
        if isinstance(kind, exp.NotNullColumnConstraint):
            nullable = False
        elif isinstance(kind, exp.PrimaryKeyColumnConstraint):
            is_pk = True
            nullable = False
        elif isinstance(kind, exp.UniqueColumnConstraint):
            unique = True
        elif isinstance(kind, exp.DefaultColumnConstraint):
            default = kind.this.sql() if kind.this else None
        elif isinstance(kind, exp.Reference):
            inline_fk = _parse_reference(kind, [col_name])
        elif kind_name in _UNMODELED_INLINE_CONSTRAINTS:
            warnings.append(
                f"column {table_name!r}.{col_name!r}: {kind_name} is not modeled and was dropped"
            )
        else:
            warnings.append(
                f"column {table_name!r}.{col_name!r}: unrecognized constraint {kind_name}"
            )

    col_type, serial_warning = _resolve_column_type(node.kind, table_name, col_name)
    if serial_warning:
        warnings.append(serial_warning)

    col = Column(
        name=col_name,
        type=col_type,
        nullable=nullable,
        unique=unique,
        default=default,
    )
    return col, is_pk, inline_fk, warnings


def _type_to_str(dt: exp.DataType) -> str:
    type_name = dt.this.value if hasattr(dt.this, "value") else str(dt.this)
    params = [e.sql() for e in dt.expressions]
    return f"{type_name}({', '.join(params)})" if params else type_name


# Postgres's SERIAL family is sugar for "INTEGER (or SMALLINT/BIGINT) column
# backed by a sequence, with DEFAULT nextval(...)" -- not a real data type in
# sqlglot's canonical vocabulary (`Column.type` validation rejects it, see
# `_validate_sql_type` in `foundation.graph`), and the identity/autoincrement
# behavior it implies is in the same "not modeled" bucket as
# `_UNMODELED_INLINE_CONSTRAINTS` above. Captured as the plain integer type;
# for the common case (a lone `id SERIAL PRIMARY KEY`), `render_ddl` already
# renders a solo unnamed PK as SQLite's `INTEGER PRIMARY KEY`, which SQLite
# treats as a rowid alias and auto-assigns on insert -- so the practical
# behavior survives even though the graph has no explicit "is identity" field.
_SERIAL_TYPES = {
    exp.DataType.Type.SERIAL: "INT",
    exp.DataType.Type.SMALLSERIAL: "SMALLINT",
    exp.DataType.Type.BIGSERIAL: "BIGINT",
}


def _resolve_column_type(dt: exp.DataType, table_name: str, col_name: str) -> tuple[str, str | None]:
    serial_type = _SERIAL_TYPES.get(dt.this)
    if serial_type is None:
        return _type_to_str(dt), None
    warning = (
        f"column {table_name!r}.{col_name!r}: Postgres {dt.this.value} is an "
        f"identity/autoincrement pseudo-type, not modeled -- captured as {serial_type!r}; "
        "the sequence/autoincrement behavior itself is dropped"
    )
    return serial_type, warning
