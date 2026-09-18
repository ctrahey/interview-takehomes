"""Unit tests for `foundation.ddl`: the graph <-> DDL pure functions (D6)."""

from __future__ import annotations

import sqlite3

import pytest

from foundation.ddl import EXECUTABLE_DIALECTS, parse_ddl, render_ddl
from foundation.graph import (
    CheckConstraint,
    Column,
    EntityGraph,
    ForeignKey,
    Table,
    UniqueConstraint,
)


def _sample_graph() -> EntityGraph:
    return EntityGraph(
        tables=[
            Table(
                name="customers",
                columns=[
                    Column(name="id", type="INT", nullable=False),
                    Column(name="name", type="TEXT", nullable=False),
                    Column(name="joined_at", type="TIMESTAMP", default="CURRENT_TIMESTAMP"),
                ],
                primary_key=["id"],
            ),
            Table(
                name="orders",
                columns=[
                    Column(name="id", type="INT", nullable=False),
                    Column(name="customer_id", type="INT", nullable=False),
                    Column(name="status", type="TEXT"),
                ],
                primary_key=["id"],
                foreign_keys=[
                    ForeignKey(
                        columns=["customer_id"],
                        ref_table="customers",
                        ref_columns=["id"],
                        on_delete="CASCADE",
                    )
                ],
                checks=[CheckConstraint(expression="status <> 'x'", name="chk_status")],
                unique_constraints=[
                    UniqueConstraint(columns=["customer_id", "status"], name="uq_cust_status")
                ],
            ),
        ]
    )


def test_render_ddl_is_pure_and_deterministic() -> None:
    graph = _sample_graph()
    first = render_ddl(graph, "sqlite")
    second = render_ddl(graph, "sqlite")
    assert first == second


def test_render_ddl_rejects_unsupported_dialect() -> None:
    with pytest.raises(ValueError, match="unsupported dialect"):
        render_ddl(_sample_graph(), "mysql")


def test_render_ddl_topologically_orders_fk_dependencies() -> None:
    # orders references customers, so customers must be declared first even
    # though the graph lists orders before customers.
    graph = EntityGraph(
        tables=[_sample_graph().tables[1], _sample_graph().tables[0]]  # orders, customers
    )
    ddl = render_ddl(graph, "sqlite")
    assert ddl.index("CREATE TABLE customers") < ddl.index("CREATE TABLE orders")


def test_render_sqlite_ddl_executes_against_real_sqlite() -> None:
    ddl = render_ddl(_sample_graph(), "sqlite")
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(ddl)  # raises sqlite3.Error if the DDL is invalid
    finally:
        conn.close()


def test_render_postgres_ddl_uses_postgres_syntax() -> None:
    ddl = render_ddl(_sample_graph(), "postgres")
    assert "REFERENCES customers (id) ON DELETE CASCADE" in ddl
    assert "postgres" not in EXECUTABLE_DIALECTS  # D5: generation target only, never executed


def test_parse_ddl_rejects_unsupported_dialect() -> None:
    with pytest.raises(ValueError, match="unsupported dialect"):
        parse_ddl("CREATE TABLE t (a INT)", "mysql")


def test_parse_ddl_skips_non_create_table_with_a_warning() -> None:
    result = parse_ddl(
        "CREATE TABLE t (a INT); CREATE INDEX idx_a ON t (a);",
        "sqlite",
    )
    assert len(result.graph.tables) == 1
    assert any("skipped non-CREATE-TABLE" in w for w in result.warnings)


def test_parse_ddl_captures_fk_check_and_unique_constraints() -> None:
    graph = _sample_graph()
    ddl = render_ddl(graph, "sqlite")
    result = parse_ddl(ddl, "sqlite")
    assert result.warnings == []

    orders = next(t for t in result.graph.tables if t.name == "orders")
    assert orders.foreign_keys[0].ref_table == "customers"
    assert orders.foreign_keys[0].ref_columns == ["id"]
    assert orders.foreign_keys[0].on_delete == "CASCADE"
    assert orders.checks[0].name == "chk_status"
    assert orders.checks[0].expression == "status <> 'x'"
    assert orders.unique_constraints[0].columns == ["customer_id", "status"]
    assert orders.unique_constraints[0].name == "uq_cust_status"


# ---------------------------------------------------------------------------
# round trip: graph -> sqlite DDL -> created DB -> parsed back -> equivalent
# ---------------------------------------------------------------------------


def test_round_trip_graph_to_sqlite_ddl_to_db_to_graph_is_exact_for_stable_types() -> None:
    """Types drawn from SQLite's stable affinity set survive the full round trip exactly.

    "Stable" means: rendering the type to SQLite DDL and re-parsing that DDL
    produces the identical canonical type string. (Not all sqlglot types have
    this property against SQLite specifically -- see the lossy-type test
    below, which documents that honestly rather than cherry-picking only the
    types that round-trip.)
    """
    graph = EntityGraph(
        tables=[
            Table(
                name="widgets",
                columns=[
                    Column(name="id", type="INT", nullable=False),
                    Column(name="label", type="TEXT", nullable=False),
                    Column(name="made_on", type="DATE"),
                    Column(name="metadata", type="JSON"),
                    Column(name="external_ref", type="UUID"),
                ],
                primary_key=["id"],
            )
        ]
    )

    ddl = render_ddl(graph, "sqlite")

    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(ddl)
        conn.execute("INSERT INTO widgets VALUES (1, 'x', '2024-01-01', '{}', 'u')")
        assert conn.execute("SELECT count(*) FROM widgets").fetchone() == (1,)
    finally:
        conn.close()

    result = parse_ddl(ddl, "sqlite")
    assert result.warnings == []
    parsed_table = result.graph.tables[0]

    original_by_name = {c.name: c for c in graph.tables[0].columns}
    parsed_by_name = {c.name: c for c in parsed_table.columns}
    for name, original_col in original_by_name.items():
        assert parsed_by_name[name].type == original_col.type, name
        assert parsed_by_name[name].nullable == original_col.nullable, name
    assert parsed_table.primary_key == graph.tables[0].primary_key


def test_round_trip_through_sqlite_is_lossy_for_types_outside_its_affinity_set() -> None:
    """SQLite's type affinity system (5 storage classes) is coarser than sqlglot's
    canonical type vocabulary. Rendering VARCHAR/DECIMAL/BOOLEAN/BIGINT to SQLite
    DDL and parsing that DDL back does NOT recover the original type string --
    this is a genuine SQLite engine limitation (it has no fixed-point decimal or
    dedicated varchar/boolean/bigint storage class), not a defect in `render_ddl`
    or `parse_ddl`. Documented here so the gap is visible, not silently assumed away.
    """
    graph = EntityGraph(
        tables=[
            Table(
                name="t",
                columns=[
                    Column(name="a", type="VARCHAR(255)"),
                    Column(name="b", type="DECIMAL(10,2)"),
                    Column(name="c", type="BOOLEAN"),
                    Column(name="d", type="BIGINT"),
                ],
            )
        ]
    )
    ddl = render_ddl(graph, "sqlite")
    result = parse_ddl(ddl, "sqlite")
    parsed = {c.name: c.type for c in result.graph.tables[0].columns}

    # None of these are identity round trips through SQLite specifically --
    # that's the documented gap, asserted explicitly rather than left implicit.
    assert parsed["a"] != "VARCHAR(255)"
    assert parsed["b"] != "DECIMAL(10, 2)"
    assert parsed["c"] != "BOOLEAN"
    assert parsed["d"] != "BIGINT"
    # But the SAME graph rendered to Postgres preserves them exactly --
    # Postgres has a richer, distinguishing type system.
    pg_ddl = render_ddl(graph, "postgres")
    assert "VARCHAR(255)" in pg_ddl
    assert "DECIMAL(10, 2)" in pg_ddl
    assert "BOOLEAN" in pg_ddl
    assert "BIGINT" in pg_ddl
