"""W18: deleting a data model takes everything derived from it, and nothing else.

The cascade decision is argued in ``foundation.deletion``'s docstring. What is
asserted here is that the decision was actually implemented -- which for a
cascade means two separate things, and only one of them is the easy one:

* **completeness** -- no row anywhere still names a deleted row, and no sample
  database file is left on disk. The orphan-hunting test walks every foreign key
  in the metadata rather than checking the tables we happened to think of, so a
  table added later cannot quietly start leaking;
* **containment** -- a second, unrelated data model in the same project is
  untouched, down to its file. A cascade that is complete and not contained is
  worse than no cascade at all.

Plus the two deliberate non-cascades: a `Query` keeps its SQL and loses its
pointers, and the append-only activity log is not touched by anything here.
"""

from __future__ import annotations

import uuid

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from foundation import deletion, paths, sample_db
from foundation.ddl import render_ddl
from foundation.graph import Column, EntityGraph, Table
from foundation.models import (
    Base,
    Corrective,
    Database,
    DataModel,
    DataModelVersion,
    Dataset,
    PendingAction,
    Query,
    Schema,
    SessionState,
    session_data_models,
)
from foundation.repositories import (
    CorrectiveRepository,
    DatabaseRepository,
    DataModelRepository,
    DataModelVersionRepository,
    DatasetRepository,
    PendingActionRepository,
    QueryRepository,
    SchemaRepository,
    SessionRepository,
    SessionStateRepository,
)

_GRAPH = EntityGraph(
    tables=[
        Table(
            name="teams",
            columns=[
                Column(name="id", type="INT", nullable=False),
                Column(name="name", type="TEXT", nullable=False),
            ],
            primary_key=["id"],
        )
    ]
)
_DDL = render_ddl(_GRAPH, "sqlite")


def _build(db: OrmSession, name: str, *, rows: int = 3, versions: int = 1) -> DataModel:
    """One data model with the full stack under it: versions, schema, database, data."""
    project = SessionRepository(db).get_default().project_id
    model = DataModelRepository(db).create(project_id=project, name=name)
    for _ in range(versions):
        version = DataModelVersionRepository(db).create(model.id, _GRAPH)
        schema = SchemaRepository(db).create(version.id, "sqlite", _DDL)
        DatasetRepository(db).create(
            version.id, f"{name}-seed", {"teams": [{"id": i, "name": f"t{i}"} for i in range(rows)]}
        )
        database_id = sample_db.create(_DDL)
        sample_db.load(database_id, {"teams": [{"id": i, "name": f"t{i}"} for i in range(rows)]})
        DatabaseRepository(db).create(schema.id, database_id)
        DatabaseRepository(db).mark_loaded(database_id)
    db.flush()
    return model


def _latest_database(db: OrmSession, model: DataModel) -> Database:
    return (
        db.execute(
            select(Database)
            .join(Schema, Database.schema_id == Schema.id)
            .join(DataModelVersion, Schema.data_model_version_id == DataModelVersion.id)
            .where(DataModelVersion.data_model_id == model.id)
            .order_by(Database.created_at.desc())
        )
        .scalars()
        .first()
    )  # type: ignore[return-value]


def _dangling_references(db: OrmSession) -> list[str]:
    """Every foreign key in the metadata store, checked against its target.

    Deliberately reflective rather than a hand-written list of the tables W18
    happens to know about. The failure mode a cascade has is *the table nobody
    thought of*, so the test has to be the kind that finds a table nobody
    thought of -- including one added after this was written.
    """
    problems: list[str] = []
    for table in Base.metadata.sorted_tables:
        for fk in table.foreign_keys:
            child, parent = fk.parent, fk.column
            orphans = db.execute(
                select(child)
                .where(child.is_not(None))
                .where(
                    child.notin_(select(parent))  # noqa: E501 - reads better on one line
                )
            ).all()
            if orphans:
                problems.append(f"{table.name}.{child.name} -> {parent.table.name}: {orphans}")
    return problems


# ---------------------------------------------------------------------------
# 1. Completeness
# ---------------------------------------------------------------------------
def test_deleting_a_model_removes_every_row_derived_from_it(db: OrmSession) -> None:
    model = _build(db, "sports-league", versions=2)
    database_ids = [
        row.id
        for row in db.execute(
            select(Database)
            .join(Schema, Database.schema_id == Schema.id)
            .join(DataModelVersion, Schema.data_model_version_id == DataModelVersion.id)
            .where(DataModelVersion.data_model_id == model.id)
        ).scalars()
    ]
    assert len(database_ids) == 2

    losses = deletion.delete_data_model(db, model.id)
    db.commit()

    assert losses is not None
    assert losses.versions == 2
    assert len(losses.schemas) == 2
    assert len(losses.databases) == 2
    assert losses.total_rows == 6

    assert db.get(DataModel, model.id) is None
    assert db.execute(select(DataModelVersion)).scalars().all() == []
    assert db.execute(select(Schema)).scalars().all() == []
    assert db.execute(select(Dataset)).scalars().all() == []
    assert db.execute(select(Database)).scalars().all() == []


def test_deleting_a_model_removes_the_sample_database_files_too(db: OrmSession) -> None:
    """Rows without files is the orphan case in its purest form: untracked bytes."""
    model = _build(db, "sports-league")
    database = _latest_database(db, model)
    path = paths.database_path(database.id)
    assert path.exists()

    deletion.delete_data_model(db, model.id)
    db.commit()

    assert not path.exists()
    assert not sample_db.exists(database.id)
    assert list(paths.managed_directory().glob("*.sqlite3")) == []


def test_nothing_anywhere_still_points_at_a_deleted_row(db: OrmSession) -> None:
    """The orphan hunt, over every foreign key the metadata declares."""
    model = _build(db, "sports-league")
    other = _build(db, "bookstore")
    session_id = SessionRepository(db).get_default().id
    database = _latest_database(db, model)
    schema_id = database.schema_id

    SessionRepository(db).attach_data_model(session_id, model.id)
    CorrectiveRepository(db).add(model.id, "revenue is in cents", session_id=session_id)
    QueryRepository(db).create(
        "SELECT 1", session_id=session_id, schema_id=schema_id, database_id=database.id
    )
    SessionStateRepository(db).set(
        session_id,
        current_data_model_id=model.id,
        current_schema_id=schema_id,
        current_database_id=database.id,
    )
    PendingActionRepository(db).request(
        session_id, action="destroy", description="...", database_id=database.id
    )
    db.commit()
    assert _dangling_references(db) == [], "the fixture itself must start clean"

    deletion.delete_data_model(db, model.id)
    db.commit()

    assert _dangling_references(db) == []
    # Specifically, and by name, each of the things the FK sweep covers:
    assert db.execute(select(Corrective)).scalars().all() == []
    assert (
        db.execute(
            select(session_data_models).where(session_data_models.c.data_model_id == model.id)
        ).all()
        == []
    )
    assert PendingActionRepository(db).get(session_id) is None
    state = db.get(SessionState, session_id)
    assert state is not None
    assert state.current_data_model_id is None
    assert state.current_schema_id is None
    assert state.current_database_id is None
    # ...and the unrelated model is still whole.
    assert db.get(DataModel, other.id) is not None


def test_a_query_keeps_its_sql_and_loses_only_its_pointers(db: OrmSession) -> None:
    """The one deliberate non-cascade. A query belongs to its session, not the model."""
    model = _build(db, "sports-league")
    session_id = SessionRepository(db).get_default().id
    database = _latest_database(db, model)
    query = QueryRepository(db).create(
        "SELECT name FROM teams",
        session_id=session_id,
        schema_id=database.schema_id,
        database_id=database.id,
        question="who plays?",
    )
    db.commit()

    losses = deletion.delete_data_model(db, model.id)
    db.commit()

    assert losses is not None
    assert losses.queries_unlinked == 1
    kept = db.get(Query, query.id)
    assert kept is not None
    assert kept.sql == "SELECT name FROM teams"
    assert kept.question == "who plays?"
    assert kept.schema_id is None
    assert kept.database_id is None


# ---------------------------------------------------------------------------
# 2. Containment
# ---------------------------------------------------------------------------
def test_an_unrelated_model_is_untouched_down_to_its_file(db: OrmSession) -> None:
    keep = _build(db, "bookstore", rows=5)
    go = _build(db, "sports-league", rows=3)
    kept_db = _latest_database(db, keep)
    kept_path = paths.database_path(kept_db.id)
    db.commit()

    deletion.delete_data_model(db, go.id)
    db.commit()

    assert db.get(DataModel, keep.id) is not None
    assert kept_path.exists()
    assert sample_db.row_counts(kept_db.id, ["teams"]) == {"teams": 5}
    assert len(db.execute(select(Database)).scalars().all()) == 1


# ---------------------------------------------------------------------------
# 3. The plan is the promise
# ---------------------------------------------------------------------------
def test_the_plan_describes_exactly_what_the_deletion_removes(db: OrmSession) -> None:
    """What the user is shown and what happens are the same object, field by field."""
    model = _build(db, "sports-league", rows=4, versions=2)
    session_id = SessionRepository(db).get_default().id
    CorrectiveRepository(db).add(model.id, "names are unique", session_id=session_id)
    db.commit()

    described = deletion.plan(db, model.id)
    assert described is not None
    assert described.name == "sports-league"
    assert described.versions == 2
    assert described.correctives == 1
    assert described.datasets == ("sports-league-seed", "sports-league-seed")
    assert described.total_rows == 8
    assert {d.row_count for d in described.databases} == {4}
    assert all(d.file_exists for d in described.databases)

    performed = deletion.delete_data_model(db, model.id)
    db.commit()
    assert performed == described


def test_planning_changes_nothing(db: OrmSession) -> None:
    model = _build(db, "sports-league")
    database = _latest_database(db, model)
    db.commit()

    deletion.plan(db, model.id)

    assert db.get(DataModel, model.id) is not None
    assert sample_db.exists(database.id)


def test_a_model_nothing_was_built_from_deletes_as_itself(db: OrmSession) -> None:
    project = SessionRepository(db).get_default().project_id
    model = DataModelRepository(db).create(project_id=project, name="empty")
    db.commit()

    described = deletion.plan(db, model.id)
    assert described is not None
    assert described.is_empty

    assert deletion.delete_data_model(db, model.id) is not None
    db.commit()
    assert db.get(DataModel, model.id) is None


def test_an_absent_model_is_none_rather_than_an_exception(db: OrmSession) -> None:
    """A careful path does not raise at the caller that is being careful."""
    missing = uuid.uuid4()
    assert deletion.plan(db, missing) is None
    assert deletion.delete_data_model(db, missing) is None


def test_a_database_whose_file_is_already_gone_is_not_a_special_case(db: OrmSession) -> None:
    model = _build(db, "sports-league")
    database = _latest_database(db, model)
    sample_db.destroy(database.id)
    db.commit()

    losses = deletion.delete_data_model(db, model.id)
    db.commit()

    assert losses is not None
    assert losses.databases[0].file_exists is False
    assert losses.total_rows == 0
    assert db.get(Database, database.id) is None


def test_the_pending_actions_of_other_sessions_go_too(db: OrmSession) -> None:
    """A live "yes" against a row that is about to vanish is not a permission."""
    model = _build(db, "sports-league")
    database = _latest_database(db, model)
    other = SessionRepository(db).create(slug="another-conversation")
    PendingActionRepository(db).request(
        other.id, action="destroy", description="...", database_id=database.id
    )
    SessionStateRepository(db).set(other.id, current_database_id=database.id)
    db.commit()

    deletion.delete_data_model(db, model.id)
    db.commit()

    assert PendingActionRepository(db).get(other.id) is None
    assert db.execute(select(PendingAction)).scalars().all() == []
    state = db.get(SessionState, other.id)
    assert state is not None and state.current_database_id is None


def test_every_orm_table_is_reachable_by_the_orphan_sweep(db: OrmSession) -> None:
    """The sweep is only worth anything if it actually covers the schema.

    Asserted rather than assumed: `_dangling_references` is reflective, so this
    pins that reflection sees the tables that matter -- otherwise a future model
    with no foreign keys registered would make the sweep silently vacuous.
    """
    covered = {
        fk.parent.table.name for table in Base.metadata.sorted_tables for fk in table.foreign_keys
    }
    assert {
        "data_model_versions",
        "schemas",
        "databases",
        "datasets",
        "queries",
        "correctives",
        "session_states",
        "pending_actions",
        "session_data_models",
    } <= covered
    assert sa_inspect(db.get_bind()).get_table_names()
