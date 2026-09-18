"""Thin, typed data-access layer over the models (design §4).

Plain functions grouped into small repository classes, one per aggregate.
No framework: each repository wraps a caller-supplied `sqlalchemy.orm.Session`
and does nothing beyond query/insert/update -- transaction boundaries stay
explicit and owned by the caller (see `foundation.db.session_scope`), which
keeps this layer trivially testable against an in-memory SQLite engine.

`project_id` and `session_id` are optional everywhere a call needs them
(MAIN.md clarification 2): omit either and the repository resolves it via
`foundation.bootstrap`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from foundation.bootstrap import get_or_create_default_project, get_or_create_default_session
from foundation.graph import EntityGraph
from foundation.models import (
    Database,
    DatabaseStatus,
    DataModel,
    DataModelVersion,
    Dataset,
    Project,
    Query,
    Schema,
)
from foundation.models import Session as SessionModel


class ProjectRepository:
    def __init__(self, db: OrmSession) -> None:
        self.db = db

    def create(self, name: str, slug: str) -> Project:
        project = Project(name=name, slug=slug)
        self.db.add(project)
        self.db.flush()
        return project

    def get(self, project_id: uuid.UUID) -> Project | None:
        return self.db.get(Project, project_id)

    def get_default(self) -> Project:
        return get_or_create_default_project(self.db)

    def list(self) -> list[Project]:
        return list(self.db.execute(select(Project).order_by(Project.created_at)).scalars())


class SessionRepository:
    def __init__(self, db: OrmSession) -> None:
        self.db = db

    def create(
        self,
        *,
        project_id: uuid.UUID | None = None,
        slug: str | None = None,
        summary: str | None = None,
    ) -> SessionModel:
        resolved_project = (
            self.db.get(Project, project_id)
            if project_id is not None
            else get_or_create_default_project(self.db)
        )
        if resolved_project is None:
            raise ValueError(f"no project with id {project_id}")
        session_row = SessionModel(project_id=resolved_project.id, slug=slug, summary=summary)
        self.db.add(session_row)
        self.db.flush()
        return session_row

    def get(self, session_id: uuid.UUID) -> SessionModel | None:
        return self.db.get(SessionModel, session_id)

    def get_default(self, project: Project | None = None) -> SessionModel:
        return get_or_create_default_session(self.db, project)

    def resolve(self, session_id: uuid.UUID | None) -> SessionModel:
        """Optional-session resolution: given id or the bootstrap default (clarification 2)."""
        if session_id is None:
            return get_or_create_default_session(self.db)
        session_row = self.db.get(SessionModel, session_id)
        if session_row is None:
            raise ValueError(f"no session with id {session_id}")
        return session_row

    def attach_data_model(self, session_id: uuid.UUID, data_model_id: uuid.UUID) -> None:
        session_row = self.db.get(SessionModel, session_id)
        model = self.db.get(DataModel, data_model_id)
        if session_row is None or model is None:
            raise ValueError("session or data model not found")
        if model not in session_row.data_models:
            session_row.data_models.append(model)
            self.db.flush()


class DataModelRepository:
    def __init__(self, db: OrmSession) -> None:
        self.db = db

    def create(self, *, project_id: uuid.UUID | None = None, name: str | None = None) -> DataModel:
        resolved_project = (
            self.db.get(Project, project_id)
            if project_id is not None
            else get_or_create_default_project(self.db)
        )
        if resolved_project is None:
            raise ValueError(f"no project with id {project_id}")
        model = DataModel(project_id=resolved_project.id, name=name)
        self.db.add(model)
        self.db.flush()
        return model

    def get(self, data_model_id: uuid.UUID) -> DataModel | None:
        return self.db.get(DataModel, data_model_id)

    def list(self, *, project_id: uuid.UUID | None = None) -> list[DataModel]:
        stmt = select(DataModel)
        if project_id is not None:
            stmt = stmt.where(DataModel.project_id == project_id)
        return list(self.db.execute(stmt.order_by(DataModel.created_at)).scalars())


class DataModelVersionRepository:
    def __init__(self, db: OrmSession) -> None:
        self.db = db

    def create(self, data_model_id: uuid.UUID, graph: EntityGraph) -> DataModelVersion:
        latest = self.db.execute(
            select(DataModelVersion.version)
            .where(DataModelVersion.data_model_id == data_model_id)
            .order_by(DataModelVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        next_version = (latest or 0) + 1
        row = DataModelVersion(
            data_model_id=data_model_id,
            version=next_version,
            graph=graph.model_dump(mode="json"),
            graph_schema_version=graph.schema_version,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def get(self, version_id: uuid.UUID) -> DataModelVersion | None:
        return self.db.get(DataModelVersion, version_id)

    def get_graph(self, version_id: uuid.UUID) -> EntityGraph:
        row = self.db.get(DataModelVersion, version_id)
        if row is None:
            raise ValueError(f"no data model version with id {version_id}")
        return EntityGraph.model_validate(row.graph)

    def latest(self, data_model_id: uuid.UUID) -> DataModelVersion | None:
        return self.db.execute(
            select(DataModelVersion)
            .where(DataModelVersion.data_model_id == data_model_id)
            .order_by(DataModelVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()


class SchemaRepository:
    def __init__(self, db: OrmSession) -> None:
        self.db = db

    def create(self, data_model_version_id: uuid.UUID, dialect: str, ddl_text: str) -> Schema:
        row = Schema(data_model_version_id=data_model_version_id, dialect=dialect, ddl=ddl_text)
        self.db.add(row)
        self.db.flush()
        return row

    def get(self, schema_id: uuid.UUID) -> Schema | None:
        return self.db.get(Schema, schema_id)

    def list_for_version(self, data_model_version_id: uuid.UUID) -> list[Schema]:
        return list(
            self.db.execute(
                select(Schema).where(Schema.data_model_version_id == data_model_version_id)
            ).scalars()
        )


class DatasetRepository:
    def __init__(self, db: OrmSession) -> None:
        self.db = db

    def create(
        self, data_model_version_id: uuid.UUID, name: str, rows: dict[str, list[dict[str, Any]]]
    ) -> Dataset:
        row = Dataset(data_model_version_id=data_model_version_id, name=name, rows=rows)
        self.db.add(row)
        self.db.flush()
        return row

    def get(self, dataset_id: uuid.UUID) -> Dataset | None:
        return self.db.get(Dataset, dataset_id)


class DatabaseRepository:
    def __init__(self, db: OrmSession) -> None:
        self.db = db

    def create(
        self, schema_id: uuid.UUID, database_id: uuid.UUID, *, engine: str = "sqlite"
    ) -> Database:
        """Record a `Database` row for a sample DB already created by `foundation.sample_db.create`.

        `database_id` is supplied by the caller (the sample_db lifecycle owns
        id generation) rather than left to the column default, so the ORM row
        and the on-disk file share the same id.
        """
        row = Database(
            id=database_id, schema_id=schema_id, engine=engine, status=DatabaseStatus.CREATED
        )
        self.db.add(row)
        self.db.flush()
        return row

    def get(self, database_id: uuid.UUID) -> Database | None:
        return self.db.get(Database, database_id)

    def mark_loaded(self, database_id: uuid.UUID) -> None:
        row = self.db.get(Database, database_id)
        if row is None:
            raise ValueError(f"no database with id {database_id}")
        row.status = DatabaseStatus.LOADED
        self.db.flush()

    def mark_destroyed(self, database_id: uuid.UUID) -> None:
        row = self.db.get(Database, database_id)
        if row is None:
            raise ValueError(f"no database with id {database_id}")
        row.status = DatabaseStatus.DESTROYED
        row.destroyed_at = datetime.now(UTC)
        self.db.flush()


class QueryRepository:
    def __init__(self, db: OrmSession) -> None:
        self.db = db

    def create(
        self,
        sql: str,
        *,
        session_id: uuid.UUID | None = None,
        schema_id: uuid.UUID | None = None,
        database_id: uuid.UUID | None = None,
        question: str | None = None,
        response_class: str | None = None,
    ) -> Query:
        session_row = SessionRepository(self.db).resolve(session_id)
        row = Query(
            session_id=session_row.id,
            schema_id=schema_id,
            database_id=database_id,
            question=question,
            sql=sql,
            response_class=response_class,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def list_for_session(self, session_id: uuid.UUID) -> list[Query]:
        stmt = select(Query).where(Query.session_id == session_id).order_by(Query.created_at)
        return list(self.db.execute(stmt).scalars())
