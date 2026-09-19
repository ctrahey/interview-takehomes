"""SQLAlchemy 2.x ORM models (design §4).

Entities: `Project`, `Session`, `DataModel`, `DataModelVersion`, `Schema`,
`Query`, `Dataset`, `Database`, plus the conversational tables
`SessionState` (pointers), `Activity` (the append-only transition log, D14)
and `PendingAction` (a destructive request awaiting a "yes", W17).
All primary keys are UUIDs (MAIN.md
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
from datetime import UTC, datetime
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


class SessionState(Base):
    """The durable "where am I?" pointers for one conversation (layer 3, W10).

    The natural-language orchestrator resolves pronouns -- "load *it* with
    data", "run *that*", "show me some rows" -- against these pointers. They
    live here, in foundation, and not in the chat client's process memory,
    because MAIN.md's whole point about Session is that the scope is *durable*:
    reopening a conversation must resume it with its current model, schema and
    database intact.

    One row per session, keyed by (and FK to) `sessions.id`. Kept as its own
    table rather than as columns on `sessions` so that `last_query_id` can be a
    real foreign key: `queries.session_id` already points at `sessions`, so the
    reverse pointer on `sessions` itself would be a table-level cycle.
    """

    __tablename__ = "session_states"

    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sessions.id"), primary_key=True
    )
    current_data_model_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("data_models.id"), nullable=True
    )
    current_data_model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("data_model_versions.id"), nullable=True
    )
    current_schema_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("schemas.id"), nullable=True
    )
    current_database_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("databases.id"), nullable=True
    )
    last_query_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("queries.id"), nullable=True
    )
    last_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = _updated_at()

    session: Mapped[Session] = relationship()


class Corrective(Base):
    """A durable domain fact the user supplied in conversation (D13).

    "revenue_cents is cents"; "cancelled orders are status='C' and are usually
    excluded". Scoped to a `DataModel` -- *not* to a session and *not* to an
    inference model -- because that is the thing the knowledge is about, and it
    must outlive both the conversation and our choice of LLM (D13's two-bucket
    split). Model correctives, which belong to an (inference_model, dialect)
    pair, are deliberately NOT stored here; they are prompt-registry material.
    """

    __tablename__ = "correctives"

    id: Mapped[uuid.UUID] = _uuid_pk()
    data_model_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("data_models.id"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sessions.id"), nullable=True, index=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created_at()

    data_model: Mapped[DataModel] = relationship()

    __table_args__ = (Index("ix_correctives_model_active", "data_model_id", "active"),)


class Activity(Base):
    """One transition in a session, appended and never updated (D14).

    D14, from Chris using the chat: *"things are really quite slow and it's hard
    to know what is going on."* The complaint is observability, and the fix is a
    record of how the session reached its current state -- not just the state.
    ``SessionState`` above is the pointers; this is the journal that produced
    them.

    **Begin and end are two rows, not one row mutated on completion.** That is
    the whole design and it is worth the extra row: a status table says a step
    is "running" and is indistinguishable from a step whose process died, while
    a transition log says a step began at 11:04:02 and never ended -- which is
    evidence. Nothing in `foundation` offers an UPDATE or DELETE path onto this
    table; `ActivityRepository` has ``append`` and readers, and that is all.

    ``seq`` is per-session and monotonic, so the log has a total order that does
    not depend on clock resolution (two activities inside one millisecond are
    common -- a `begin` and its `end` for a cached read, for instance).

    ``model`` / ``tokens`` / ``request_id`` are populated only where an
    inference call was involved, which is what lets the eval harness attribute
    wall-clock to providers rather than guessing.
    """

    __tablename__ = "activities"

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sessions.id"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    phase: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict | None] = mapped_column(SA_JSON, nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    session: Mapped[Session] = relationship()

    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_activities_session_seq"),
        Index("ix_activities_session_seq", "session_id", "seq"),
        Index("ix_activities_session_kind", "session_id", "kind"),
    )


class PendingAction(Base):
    """A destructive action that has been described to the user and is awaiting a "yes".

    W17. Destroying a sample database is the first thing layer 3 can do that is
    not undoable, so it is the first thing that must not happen on a single
    utterance. The rule is: **describe precisely, then require an affirmative in
    the next turn.** That requirement only means anything if the request
    survives the turn boundary, which is what this row is.

    Deliberately *not* the activity log. `Activity` is append-only because it is
    a record of the past; this is live state with exactly three transitions --
    created, consumed, invalidated -- and it must be deletable, because a
    pending confirmation that outlives the user's attention is precisely the
    hazard. Both are written: the log records that destruction was asked for and
    what came of it (D14), and this row is what makes the next "yes" meaningful.

    One row per session (the PK *is* `session_id`): a second request replaces
    the first, so "delete A" / "no wait, delete B" / "yes" can only ever destroy
    B. `requested_seq` pins the request to the activity row that described it,
    so the log and the pending state cannot disagree about which destruction was
    approved.

    W18 adds a second kind of row to the same table, and the distinction is
    carried entirely by `action`. `destroy` / `destroy_model` / `clear_data` are
    *permissions*: an affirmative next turn executes them. `destroy_scope` is a
    *question* -- "did you mean the model or its database?" -- and is answered
    with a scope, never with a yes. Nothing consumes it as consent, because the
    handler for a destructive action only ever acts on a row whose `action` is
    that exact action.
    """

    __tablename__ = "pending_actions"

    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sessions.id"), primary_key=True
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    database_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("databases.id"), nullable=True
    )
    #: W18. A destructive request names a database, a data model, or (while the
    #: scope question is outstanding) a model whose scope is not settled yet.
    #: Two nullable columns rather than one polymorphic id, because a foreign
    #: key that sometimes points at another table is a foreign key the database
    #: cannot check -- and this row's entire purpose is to be trustworthy.
    data_model_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("data_models.id"), nullable=True
    )
    #: The exact sentence the user was shown. Replayed verbatim on confirmation
    #: so what is destroyed is what was described, not a re-derivation of it.
    description: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict | None] = mapped_column(SA_JSON, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    requested_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)

    session: Mapped[Session] = relationship()
