"""Unit tests for `foundation.graph`: the pydantic entity graph schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundation.graph import GRAPH_SCHEMA_VERSION, Column, EntityGraph, Table


def test_entity_graph_defaults_to_current_schema_version() -> None:
    graph = EntityGraph(tables=[])
    assert graph.schema_version == GRAPH_SCHEMA_VERSION


def test_column_rejects_unrecognized_type() -> None:
    with pytest.raises(ValidationError, match="not a recognized SQL type"):
        Column(name="x", type="NOT_A_REAL_TYPE_XYZ")


def test_column_accepts_parameterized_types() -> None:
    col = Column(name="x", type="VARCHAR(255)")
    assert col.type == "VARCHAR(255)"


def test_entity_graph_round_trips_through_json() -> None:
    col = Column(name="a", type="INT", nullable=False)
    graph = EntityGraph(tables=[Table(name="t", columns=[col], primary_key=["a"])])
    restored = EntityGraph.model_validate_json(graph.model_dump_json())
    assert restored == graph
