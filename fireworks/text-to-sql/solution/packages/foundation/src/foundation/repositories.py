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

from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from foundation.bootstrap import get_or_create_default_project, get_or_create_default_session
from foundation.graph import EntityGraph
from foundation.models import (
    Activity,
    Corrective,
    Database,
    DatabaseStatus,
    DataModel,
    DataModelVersion,
    Dataset,
    Project,
    Query,
    Schema,
    SessionState,
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


class SessionStateRepository:
    """The durable "current" pointers for a conversation (`models.SessionState`).

    Used by the layer-3 orchestrator so pronouns resolve across process
    restarts: "load it with data" means the current schema, "run that" means
    the last query. Nothing here interprets natural language -- it stores and
    returns ids.
    """

    def __init__(self, db: OrmSession) -> None:
        self.db = db

    def get_or_create(self, session_id: uuid.UUID) -> SessionState:
        existing = self.db.get(SessionState, session_id)
        if existing is not None:
            return existing
        if self.db.get(SessionModel, session_id) is None:
            raise ValueError(f"no session with id {session_id}")
        row = SessionState(session_id=session_id)
        self.db.add(row)
        self.db.flush()
        return row

    def set(self, session_id: uuid.UUID, **pointers: uuid.UUID | str | None) -> SessionState:
        """Set one or more pointers. Only the keys passed are touched.

        Passing an explicit ``None`` clears that pointer; omitting the key
        leaves it alone. That distinction matters -- "I destroyed the database"
        has to be expressible without also clearing the current schema.
        """
        row = self.get_or_create(session_id)
        allowed = {
            "current_data_model_id",
            "current_data_model_version_id",
            "current_schema_id",
            "current_database_id",
            "last_query_id",
            "last_question",
        }
        unknown = set(pointers) - allowed
        if unknown:
            raise ValueError(f"unknown session state pointer(s): {sorted(unknown)}")
        for key, value in pointers.items():
            setattr(row, key, value)
        self.db.flush()
        return row


class CorrectiveRepository:
    """Domain correctives, scoped to a `DataModel` (D13)."""

    #: A corrective is reference data in a *system* prompt, which is the most
    #: privileged position in the request. Bound both the number carried and
    #: the length of each one, so a single conversation cannot grow the system
    #: prompt without limit.
    MAX_TEXT_LENGTH = 400

    def __init__(self, db: OrmSession) -> None:
        self.db = db

    def add(
        self,
        data_model_id: uuid.UUID,
        text: str,
        *,
        session_id: uuid.UUID | None = None,
    ) -> Corrective:
        cleaned = " ".join(text.split())
        if not cleaned:
            raise ValueError("a corrective cannot be empty")
        if len(cleaned) > self.MAX_TEXT_LENGTH:
            raise ValueError(
                f"corrective is {len(cleaned)} characters; the limit is {self.MAX_TEXT_LENGTH}"
            )
        if self.db.get(DataModel, data_model_id) is None:
            raise ValueError(f"no data model with id {data_model_id}")
        row = Corrective(data_model_id=data_model_id, session_id=session_id, text=cleaned)
        self.db.add(row)
        self.db.flush()
        return row

    def list_active(self, data_model_id: uuid.UUID) -> list[Corrective]:
        stmt = (
            select(Corrective)
            .where(Corrective.data_model_id == data_model_id, Corrective.active.is_(True))
            .order_by(Corrective.created_at)
        )
        return list(self.db.execute(stmt).scalars())

    def deactivate(self, corrective_id: uuid.UUID) -> None:
        row = self.db.get(Corrective, corrective_id)
        if row is None:
            raise ValueError(f"no corrective with id {corrective_id}")
        row.active = False
        self.db.flush()


class ActivityRepository:
    """The append-only session activity log (D14).

    **This class deliberately has no update and no delete.** That is the
    invariant, not an oversight: an activity row records that a transition
    happened at a moment, and a record of the past that can be rewritten is not
    a record. A step that completes is a *second* row (`phase="end"`), which is
    also what makes a crashed step legible -- its `begin` stands alone with no
    `end`, instead of a mutable row stuck forever at "running".

    `seq` is allocated per session as ``max(seq) + 1`` inside the caller's
    transaction. One conversation is one writer, so this is not a contended
    allocation; the unique constraint on ``(session_id, seq)`` turns the
    concurrent case into a loud integrity error rather than a silently
    reordered log.
    """

    #: Phases and statuses, named here so callers and tests agree on spelling.
    BEGIN = "begin"
    END = "end"
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"
    REFUSED = "refused"

    def __init__(self, db: OrmSession) -> None:
        self.db = db

    def next_seq(self, session_id: uuid.UUID) -> int:
        current = self.db.execute(
            select(func.max(Activity.seq)).where(Activity.session_id == session_id)
        ).scalar_one_or_none()
        return int(current or 0) + 1

    def append(
        self,
        session_id: uuid.UUID,
        *,
        kind: str,
        phase: str,
        status: str = RUNNING,
        summary: str | None = None,
        detail: dict[str, Any] | None = None,
        duration_ms: int | None = None,
        model: str | None = None,
        tokens: int | None = None,
        request_id: str | None = None,
        at: datetime | None = None,
    ) -> Activity:
        if phase not in (self.BEGIN, self.END):
            raise ValueError(f"phase must be 'begin' or 'end', not {phase!r}")
        row = Activity(
            session_id=session_id,
            seq=self.next_seq(session_id),
            kind=kind,
            phase=phase,
            status=status,
            at=at or datetime.now(UTC),
            duration_ms=duration_ms,
            summary=summary,
            detail=detail,
            model=model,
            tokens=tokens,
            request_id=request_id,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def list_for_session(
        self, session_id: uuid.UUID, *, limit: int | None = None
    ) -> list[Activity]:
        """The log in `seq` order. ``limit`` returns the most recent N, still ordered."""
        stmt = select(Activity).where(Activity.session_id == session_id)
        if limit is None:
            return list(self.db.execute(stmt.order_by(Activity.seq)).scalars())
        tail = list(self.db.execute(stmt.order_by(Activity.seq.desc()).limit(limit)).scalars())
        return list(reversed(tail))

    def latest(
        self,
        session_id: uuid.UUID,
        *,
        kind: str,
        phase: str = END,
        status: str | None = OK,
    ) -> Activity | None:
        """The most recent activity of one kind -- how a referent is resolved (D15).

        "what's the SQL for *that*?" is answered by the last successful
        ``query.generate`` end-row, read out of this table. No model is asked
        what "that" referred to, because the log already knows.
        """
        stmt = select(Activity).where(Activity.session_id == session_id, Activity.kind == kind)
        if phase is not None:
            stmt = stmt.where(Activity.phase == phase)
        if status is not None:
            stmt = stmt.where(Activity.status == status)
        return self.db.execute(stmt.order_by(Activity.seq.desc()).limit(1)).scalar_one_or_none()

    def unfinished(self, session_id: uuid.UUID) -> list[Activity]:
        """`begin` rows with no matching `end` -- steps that never completed.

        A crashed process leaves exactly this. It is the reason the log is two
        rows per step: there is no equivalent question to ask of a status table
        whose "running" row was never updated.
        """
        by_kind: dict[str, list[Activity]] = {}
        for row in self.list_for_session(session_id):
            by_kind.setdefault(row.kind, []).append(row)
        open_rows: list[Activity] = []
        for group in by_kind.values():
            # Steps of one kind nest at most by recursion we do not do, so
            # first-in/first-out pairing within a kind is exact.
            pending: list[Activity] = []
            for row in group:
                if row.phase == self.BEGIN:
                    pending.append(row)
                elif pending:
                    pending.pop(0)
            open_rows.extend(pending)
        return sorted(open_rows, key=lambda r: r.seq)
