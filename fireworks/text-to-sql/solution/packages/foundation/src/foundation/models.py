"""SQLAlchemy 2.x ORM models (design §4).

Entities: `Project`, `Session`, `DataModel`, `DataModelVersion`, `Schema`,
`Query`, `Dataset`, `Database`. All primary keys are UUIDs (MAIN.md
clarification 3), stored via SQLAlchemy's portable `Uuid` type.

Relationships, derived from `prompts/MAIN.md`'s domain-object descriptions:

- `Project` is the top-level container. `Session` and `DataModel` both belong
  to exactly one `Project`.
- `DataModel` is versioned: each `DataModelVersion` holds one immutable
  `EntityGraph` snapshot (`foundation.graph.EntityGraph`, D6) plus a
  monotonically increasing `version` number per model.
- `Schema` is a concrete DDL rendering of one `DataModelVersion` for one
  dialect/engine (D6) -- a pragmatic projection of the purist graph.
- `Dataset` is importable sample data, associated with the `DataModelVersion`
  it was authored against (so it's clear which graph its rows are meant to
  satisfy).
- `Database` is a running (or destroyed) instance of an RDBMS, created from
  one `Schema`. Its `path` is NEVER client-supplied (D9) -- see
  `foundation.paths` and `foundation.sample_db`.
- `Query` is a piece of SQL, usually system-generated, optionally tied to a
  `Session` (the conversation it came from) and/or a `Database` it was run
  against.
- `Session` <-> `DataModel` is many-to-many ("a session/conversation MAY
  refer to any number of models (zero-or-more)", MAIN.md) via the
  `session_data_models` association table.

Every FK-bearing column and every natural lookup path (project slug, model
name, dataset/database status) has an index -- see the `Index(...)` /
`index=True` declarations below.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON as SA_JSON
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from sqlalchemy.types import Uuid


class Base(DeclarativeBase):
    """Declarative base shared by every foundation model."""


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class DatabaseStatus(StrEnum):
    CREATED = "created"
    LOADED = "loaded"
    DESTROYED = "destroyed"


session_data_models = Table(
    "session_data_models",
    Base.metadata,
    Column("session_id", Uuid(as_uuid=True), ForeignKey("sessions.id"), primary_key=True),
    Column("data_model_id", Uuid(as_uuid=True), ForeignKey("data_models.id"), primary_key=True),
)


class Project(Base):
    """Container that holds related models and session history for a user (MAIN.md)."""

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    sessions: Mapped[list[Session]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    data_models: Mapped[list[DataModel]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Session(Base):
    """Logically a conversation; durable scope, may span multiple data models (MAIN.md)."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    slug: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    project: Mapped[Project] = relationship(back_populates="sessions")
    data_models: Mapped[list[DataModel]] = relationship(
        secondary=session_data_models, back_populates="sessions"
    )
    queries: Mapped[list[Query]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_sessions_project_default", "project_id", "is_default"),
        UniqueConstraint("project_id", "slug", name="uq_sessions_project_slug"),
    )


class DataModel(Base):
    """Logical, dialect-neutral schema-under-discussion. Versioned, optionally named (MAIN.md)."""

    __tablename__ = "data_models"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    project: Mapped[Project] = relationship(back_populates="data_models")
    sessions: Mapped[list[Session]] = relationship(
        secondary=session_data_models, back_populates="data_models"
    )
    versions: Mapped[list[DataModelVersion]] = relationship(
        back_populates="data_model",
        cascade="all, delete-orphan",
        order_by="DataModelVersion.version",
    )


class DataModelVersion(Base):
    """One immutable snapshot of a `DataModel`'s dialect-neutral entity graph (D6)."""

    __tablename__ = "data_model_versions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    data_model_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("data_models.id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    graph: Mapped[dict] = mapped_column(SA_JSON, nullable=False)
    graph_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _created_at()

    data_model: Mapped[DataModel] = relationship(back_populates="versions")
    schemas: Mapped[list[Schema]] = relationship(
        back_populates="data_model_version", cascade="all, delete-orphan"
    )
    datasets: Mapped[list[Dataset]] = relationship(
        back_populates="data_model_version", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("data_model_id", "version", name="uq_data_model_versions_model_version"),
        Index("ix_data_model_versions_model_version", "data_model_id", "version"),
    )


class Schema(Base):
    """A concrete DDL projection of one `DataModelVersion` for one engine dialect (D6)."""

    __tablename__ = "schemas"

    id: Mapped[uuid.UUID] = _uuid_pk()
    data_model_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("data_model_versions.id"), nullable=False, index=True
    )
    dialect: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    ddl: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()

    data_model_version: Mapped[DataModelVersion] = relationship(back_populates="schemas")
    databases: Mapped[list[Database]] = relationship(
        back_populates="schema", cascade="all, delete-orphan"
    )
    queries: Mapped[list[Query]] = relationship(back_populates="schema")

    __table_args__ = (Index("ix_schemas_version_dialect", "data_model_version_id", "dialect"),)


class Dataset(Base):
    """Importable sample data ("conformant or intentionally maligned", MAIN.md)."""

    __tablename__ = "datasets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    data_model_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("data_model_versions.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    rows: Mapped[dict] = mapped_column(
        SA_JSON, nullable=False, doc="{table_name: [ {col: value, ...}, ... ]}"
    )
    created_at: Mapped[datetime] = _created_at()

    data_model_version: Mapped[DataModelVersion] = relationship(back_populates="datasets")


class Database(Base):
    """A running (or destroyed) sample RDBMS instance created from a `Schema` (D9).

    `path` is derived exclusively from `id` by `foundation.paths` -- nothing
    client-supplied ever becomes part of a filesystem path.
    """

    __tablename__ = "databases"

    id: Mapped[uuid.UUID] = _uuid_pk()
    schema_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("schemas.id"), nullable=False, index=True
    )
    engine: Mapped[str] = mapped_column(String(32), nullable=False, default="sqlite")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=DatabaseStatus.CREATED, index=True
    )
    created_at: Mapped[datetime] = _created_at()
    destroyed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    schema: Mapped[Schema] = relationship(back_populates="databases")
    queries: Mapped[list[Query]] = relationship(back_populates="database")


class Query(Base):
    """A piece of SQL, usually an output of the text-to-SQL system (MAIN.md)."""

    __tablename__ = "queries"

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sessions.id"), nullable=False, index=True
    )
    schema_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("schemas.id"), nullable=True, index=True
    )
    database_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("databases.id"), nullable=True, index=True
    )
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    sql: Mapped[str] = mapped_column(Text, nullable=False)
    response_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = _created_at()

    session: Mapped[Session] = relationship(back_populates="queries")
    schema: Mapped[Schema | None] = relationship(back_populates="queries")
    database: Mapped[Database | None] = relationship(back_populates="queries")
