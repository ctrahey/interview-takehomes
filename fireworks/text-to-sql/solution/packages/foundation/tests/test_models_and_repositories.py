"""Tests for the SQLAlchemy models, default-project/session bootstrap, and repositories."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session as OrmSession

from foundation.bootstrap import get_or_create_default_project, get_or_create_default_session
from foundation.graph import Column, EntityGraph, Table
from foundation.models import Project
from foundation.repositories import (
    DatabaseRepository,
    DataModelRepository,
    DataModelVersionRepository,
    DatasetRepository,
    ProjectRepository,
    QueryRepository,
    SchemaRepository,
    SessionRepository,
)


def test_project_and_session_have_uuid_primary_keys(db: OrmSession) -> None:
    project = ProjectRepository(db).create(name="Acme", slug="acme")
    session_row = SessionRepository(db).create(project_id=project.id)
    assert isinstance(project.id, uuid.UUID)
    assert isinstance(session_row.id, uuid.UUID)


def test_default_project_bootstrap_is_idempotent(db: OrmSession) -> None:
    first = get_or_create_default_project(db)
    second = get_or_create_default_project(db)
    assert first.id == second.id
    assert db.query(Project).filter(Project.slug == "default").count() == 1


def _simple_graph() -> EntityGraph:
    col = Column(name="a", type="INT", nullable=False)
    return EntityGraph(tables=[Table(name="t", columns=[col], primary_key=["a"])])


def test_default_session_bootstrap_is_idempotent_and_scoped_to_default_project(
    db: OrmSession,
) -> None:
    first = get_or_create_default_session(db)
    second = get_or_create_default_session(db)
    assert first.id == second.id
    assert first.project_id == get_or_create_default_project(db).id


def test_repositories_fall_back_to_default_project_and_session_when_omitted(
    db: OrmSession,
) -> None:
    model = DataModelRepository(db).create(name="orders model")  # no project_id given
    assert model.project_id == get_or_create_default_project(db).id

    query_row = QueryRepository(db).create("SELECT 1")  # no session_id given
    assert query_row.session_id == get_or_create_default_session(db).id


def test_data_model_version_persists_and_reloads_graph(db: OrmSession) -> None:
    model = DataModelRepository(db).create(name="m")
    graph = _simple_graph()
    version_repo = DataModelVersionRepository(db)
    version = version_repo.create(model.id, graph)
    assert version.version == 1

    reloaded = version_repo.get_graph(version.id)
    assert reloaded == graph

    version2 = version_repo.create(model.id, graph)
    assert version2.version == 2
    latest = version_repo.latest(model.id)
    assert latest is not None
    assert latest.id == version2.id


def test_schema_is_a_projection_of_a_data_model_version(db: OrmSession) -> None:
    model = DataModelRepository(db).create(name="m")
    graph = _simple_graph()
    version = DataModelVersionRepository(db).create(model.id, graph)

    ddl_text = "CREATE TABLE t (a INTEGER PRIMARY KEY);"
    schema_row = SchemaRepository(db).create(version.id, "sqlite", ddl_text)
    assert schema_row.data_model_version_id == version.id
    assert schema_row.dialect == "sqlite"


def test_dataset_belongs_to_a_data_model_version(db: OrmSession) -> None:
    model = DataModelRepository(db).create(name="m")
    graph = _simple_graph()
    version = DataModelVersionRepository(db).create(model.id, graph)
    dataset = DatasetRepository(db).create(version.id, "seed", {"t": [{"a": 1}, {"a": 2}]})
    assert dataset.rows["t"] == [{"a": 1}, {"a": 2}]


def test_database_lifecycle_status_transitions(db: OrmSession) -> None:
    model = DataModelRepository(db).create(name="m")
    graph = _simple_graph()
    version = DataModelVersionRepository(db).create(model.id, graph)
    ddl_text = "CREATE TABLE t (a INTEGER PRIMARY KEY);"
    schema_row = SchemaRepository(db).create(version.id, "sqlite", ddl_text)

    db_repo = DatabaseRepository(db)
    database_id = uuid.uuid4()
    database = db_repo.create(schema_row.id, database_id)
    assert database.status == "created"

    db_repo.mark_loaded(database_id)
    loaded = db_repo.get(database_id)
    assert loaded is not None
    assert loaded.status == "loaded"

    db_repo.mark_destroyed(database_id)
    reloaded = db_repo.get(database_id)
    assert reloaded is not None
    assert reloaded.status == "destroyed"
    assert reloaded.destroyed_at is not None


def test_session_can_reference_multiple_data_models(db: OrmSession) -> None:
    project = ProjectRepository(db).create(name="p", slug="p")
    session_row = SessionRepository(db).create(project_id=project.id)
    model_a = DataModelRepository(db).create(project_id=project.id, name="a")
    model_b = DataModelRepository(db).create(project_id=project.id, name="b")

    SessionRepository(db).attach_data_model(session_row.id, model_a.id)
    SessionRepository(db).attach_data_model(session_row.id, model_b.id)
    db.expire_all()

    reloaded = SessionRepository(db).get(session_row.id)
    assert reloaded is not None
    assert {m.id for m in reloaded.data_models} == {model_a.id, model_b.id}


def test_session_resolve_raises_for_unknown_id(db: OrmSession) -> None:
    with pytest.raises(ValueError, match="no session"):
        SessionRepository(db).resolve(uuid.uuid4())
