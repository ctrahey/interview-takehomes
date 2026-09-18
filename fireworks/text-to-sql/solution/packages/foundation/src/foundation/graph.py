"""The dialect-neutral entity graph (D6).

`DataModelVersion.graph` (see `foundation.models.DataModelVersion`) stores an
`EntityGraph` as JSON. It is the "purist" data model: tables, columns, types,
primary keys, foreign keys, and constraints, expressed independently of any
one SQL dialect. `foundation.ddl.render_ddl` projects it to concrete DDL for
one engine (a `Schema` row, see `foundation.models.Schema`);
`foundation.ddl.parse_ddl` is the (partial) inverse, letting a user-supplied
DDL script be captured as a graph.

Column and expression types are spelled using sqlglot's own canonical type
vocabulary (``sqlglot.exp.DataType.Type``, e.g. ``"INT"``, ``"VARCHAR(255)"``,
``"DECIMAL(10,2)"``, ``"TIMESTAMP"``) rather than a bespoke enum. sqlglot's
type system already *is* a dialect-neutral canonical form used for
cross-dialect transpilation, so reusing it here means `render_ddl` really is
"build the sqlglot AST, then call ``.sql(dialect=...)``" — a pure function,
not a template engine in a trenchcoat.

Default and CHECK expressions are stored as raw SQL text (dialect-generic;
see the docstring of `foundation.ddl` for the transpilation caveat this
implies).

Schema versioning: bump `GRAPH_SCHEMA_VERSION` and add a migration note here
whenever the shape of `EntityGraph` changes in a way that is not purely
additive. Persisted graphs carry their own `schema_version` field so old rows
remain interpretable (and a loader can branch on it) even after later bumps.

Deliberately NOT modeled (kept out of the graph on purpose, not by oversight):
indexes (other than the implicit ones a PK/UNIQUE creates), views, triggers,
stored procedures, table partitioning, generated/computed columns, sequences,
and per-dialect storage/engine options (e.g. MySQL ``ENGINE=InnoDB``). These
are dialect- or workload-specific concerns that do not belong in a purist,
dialect-neutral entity graph; a `Schema` rendering may add them later as a
pragmatic, engine-specific concern without disturbing the graph.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlglot import exp
from sqlglot.errors import ParseError

GRAPH_SCHEMA_VERSION = 1


def _validate_sql_type(value: str) -> str:
    try:
        exp.DataType.build(value)
    except (ParseError, ValueError) as exc:
        raise ValueError(f"not a recognized SQL type: {value!r}") from exc
    return value


class Column(BaseModel):
    """One column of one table."""

    model_config = {"frozen": False}

    name: str
    type: str = Field(
        description="sqlglot canonical type, e.g. 'INT', 'VARCHAR(255)', 'DECIMAL(10,2)'."
    )
    nullable: bool = True
    unique: bool = False
    default: str | None = Field(
        default=None,
        description="Raw SQL expression text, e.g. 'CURRENT_TIMESTAMP' or '0'.",
    )
    comment: str | None = None

    @field_validator("type")
    @classmethod
    def _type_is_known(cls, v: str) -> str:
        return _validate_sql_type(v)


class ForeignKey(BaseModel):
    columns: list[str]
    ref_table: str
    ref_columns: list[str]
    name: str | None = None
    on_delete: str | None = Field(
        default=None, description="CASCADE | SET NULL | RESTRICT | NO ACTION"
    )
    on_update: str | None = Field(
        default=None, description="CASCADE | SET NULL | RESTRICT | NO ACTION"
    )


class CheckConstraint(BaseModel):
    expression: str = Field(description="Raw boolean SQL expression, e.g. 'total >= 0'.")
    name: str | None = None


class UniqueConstraint(BaseModel):
    """A multi-column (or explicitly named single-column) UNIQUE constraint.

    A single unnamed single-column uniqueness requirement should be spelled
    with ``Column.unique = True`` instead; this class exists for the
    multi-column and/or named cases that don't fit on the column.
    """

    columns: list[str]
    name: str | None = None


class Table(BaseModel):
    name: str
    columns: list[Column]
    primary_key: list[str] = Field(default_factory=list)
    primary_key_name: str | None = None
    foreign_keys: list[ForeignKey] = Field(default_factory=list)
    checks: list[CheckConstraint] = Field(default_factory=list)
    unique_constraints: list[UniqueConstraint] = Field(default_factory=list)
    comment: str | None = None

    @model_validator(mode="after")
    def _primary_key_columns_are_not_nullable(self) -> Table:
        # SQL standard: a PRIMARY KEY column is implicitly NOT NULL. Enforcing
        # this in the graph (rather than silently coercing it at render time)
        # keeps the graph the single source of truth for a column's real
        # nullability, and catches an inconsistent hand-authored model early.
        by_name = {c.name: c for c in self.columns}
        for name in self.primary_key:
            col = by_name.get(name)
            if col is None:
                raise ValueError(
                    f"primary_key references unknown column {name!r} on table {self.name!r}"
                )
            if col.nullable:
                raise ValueError(
                    f"primary key column {self.name!r}.{name!r} must have nullable=False"
                )
        return self


class EntityGraph(BaseModel):
    """A dialect-neutral entity graph: the full contents of a `DataModelVersion`."""

    schema_version: int = GRAPH_SCHEMA_VERSION
    tables: list[Table]
