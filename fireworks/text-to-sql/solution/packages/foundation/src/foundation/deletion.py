"""Deleting a `DataModel`, and saying in advance exactly what that costs (W18).

W17 gave layer 3 a way to delete a sample *database*. It gave it no way at all
to delete a *data model*, which is the thing users actually name -- Chris asked
to "delete the sports model", was asked "the model, the database, or something
else?", answered "both", and the system could not honour either half. This
module is the missing half.

## The cascade decision, and why

A `DataModel` owns versions; a version owns schemas and datasets; a schema owns
sample databases; a sample database owns a file on disk. Deleting the model has
to decide what happens to all of it, and there are only three coherent answers:

1. **refuse while anything hangs off it** -- safe, and useless: the model a user
   wants rid of is precisely the one they have been building on;
2. **detach and keep the children** -- leaves a sample database whose schema,
   version and model are gone. It is unreachable through every listing in this
   system (all of them start from the project's data models) and its file stays
   on disk forever. That is an orphan the user can neither see nor address;
3. **cascade** -- everything derived from the model goes with it.

We cascade, and the reason is (2)'s failure mode rather than any tidiness
argument: an object the user cannot see is an object they cannot delete, and a
workbench whose "delete" quietly accumulates invisible files is worse than one
with no delete at all. The cost of cascading is that it is a big, irreversible
operation -- which is paid for by describing it first (:func:`plan` exists for
exactly that) and requiring an explicit confirmation of *that description*.

A second, quieter payoff: it makes "both" -- delete the model AND its database
-- a thing the system can actually do, because deleting the model already
deletes the databases. The question the router asks has two executable answers
instead of one.

## What is NOT cascaded, and why that is not a dangling FK

* **`Query` rows survive**, with `schema_id` and `database_id` set to NULL.
  A query's mandatory owner is its `Session`, not the model: it is the record
  of what the user asked, and rewriting their conversation is not part of
  deleting a design. Both pointers are nullable by declaration, so nulling them
  is the schema's own way of saying "written against something that is gone".
  Said out loud in the plan, never silently.
* **`Activity` rows survive**, because D14 makes the log append-only. It
  references only its session, so nothing dangles; and the record that a model
  was deleted is the one record that must outlive the model.

Everything else that points into the subtree is removed or cleared, and there
is a test that walks every foreign key in the metadata and asserts nothing
still names a deleted row.

## Files as well as rows

`sample_db.destroy` is called for every database in the subtree. Deleting the
`Database` row and leaving the file is the orphan of case (2) in its purest
form -- an untracked SQLite file under the managed directory that nothing in
the system can ever name again.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete as sql_delete
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.sql.elements import ColumnElement

from foundation import sample_db
from foundation.graph import EntityGraph
from foundation.models import (
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

__all__ = [
    "DatabaseLoss",
    "ModelDeletion",
    "SchemaLoss",
    "delete_data_model",
    "plan",
]


@dataclass(frozen=True, slots=True)
class SchemaLoss:
    """One concrete DDL rendering that goes with the model."""

    schema_id: uuid.UUID
    dialect: str
    version: int

    @property
    def short_id(self) -> str:
        return str(self.schema_id)[:8]

    @property
    def label(self) -> str:
        return f"{self.short_id} (v{self.version}, {self.dialect})"


@dataclass(frozen=True, slots=True)
class DatabaseLoss:
    """One sample database that goes with the model, and what is in it *now*.

    The row count is read out of the file rather than out of any stored dataset
    record, for the same reason W17 reads it for a single-database destroy: a
    confirmation that describes what we believe was loaded, when the truth is on
    disk and cheap to read, can be wrong about the one thing it exists to
    protect.
    """

    database_id: uuid.UUID
    status: str
    tables: tuple[str, ...] = ()
    row_count: int = 0
    file_exists: bool = False

    @property
    def short_id(self) -> str:
        return str(self.database_id)[:8]

    @property
    def label(self) -> str:
        if not self.file_exists:
            return f"{self.short_id} (no file on disk)"
        return f"{self.short_id} ({self.row_count} row(s) across {len(self.tables)} table(s))"


@dataclass(frozen=True, slots=True)
class ModelDeletion:
    """Everything one `delete_data_model` call would remove, or did remove.

    The same type is returned by :func:`plan` and by :func:`delete_data_model`,
    deliberately: the confirmation a user is shown and the outcome that is
    recorded are then comparable field by field, and a test can assert that what
    was described is what happened.
    """

    data_model_id: uuid.UUID
    name: str
    versions: int = 0
    schemas: tuple[SchemaLoss, ...] = ()
    databases: tuple[DatabaseLoss, ...] = ()
    datasets: tuple[str, ...] = ()
    correctives: int = 0
    #: `Query` rows that point into this subtree. They are kept; their
    #: `schema_id` / `database_id` are cleared. Counted so the description can
    #: say so rather than leaving the user to discover it.
    queries_unlinked: int = 0
    #: Sessions whose "current" pointers name something in this subtree and are
    #: therefore reset. Includes sessions other than the one doing the deleting.
    sessions_reset: int = 0
    _files: tuple[uuid.UUID, ...] = field(default=(), repr=False)

    @property
    def total_rows(self) -> int:
        return sum(d.row_count for d in self.databases)

    @property
    def live_databases(self) -> tuple[DatabaseLoss, ...]:
        """Those that still have a file on disk -- what "and the data" means."""
        return tuple(d for d in self.databases if d.file_exists)

    @property
    def is_empty(self) -> bool:
        """True when nothing but the model row itself would go.

        Worth naming: "delete a model nothing was ever built from" is a small,
        ordinary act, and describing it with the same blast-radius paragraph as
        a model with five databases trains users to skim the paragraph.
        """
        return not (self.versions or self.schemas or self.databases or self.datasets)


def plan(db: OrmSession, data_model_id: uuid.UUID) -> ModelDeletion | None:
    """Enumerate what deleting this model would cost. Changes nothing.

    Returns ``None`` when there is no such model -- an absent model is a
    question for the caller to ask, not an exception to raise on a path whose
    whole job is to be careful.
    """
    model = db.get(DataModel, data_model_id)
    if model is None:
        return None

    versions = list(
        db.execute(
            select(DataModelVersion)
            .where(DataModelVersion.data_model_id == data_model_id)
            .order_by(DataModelVersion.version)
        ).scalars()
    )
    version_ids = [v.id for v in versions]
    tables_by_version = {v.id: _tables_of(v) for v in versions}

    schema_rows = _schemas_of(db, version_ids)
    schemas = tuple(
        SchemaLoss(schema_id=row.id, dialect=row.dialect, version=version_number)
        for row, version_number in schema_rows
    )
    schema_ids = [row.id for row, _ in schema_rows]

    databases = tuple(
        _database_loss(row, tables_by_version.get(version_id, ()))
        for row, version_id in _databases_of(db, schema_ids)
    )

    datasets = (
        tuple(
            db.execute(
                select(Dataset.name)
                .where(Dataset.data_model_version_id.in_(version_ids))
                .order_by(Dataset.created_at)
            ).scalars()
        )
        if version_ids
        else ()
    )

    correctives = len(
        db.execute(select(Corrective.id).where(Corrective.data_model_id == data_model_id))
        .scalars()
        .all()
    )

    database_ids = [d.database_id for d in databases]
    queries = _linked_queries(db, schema_ids, database_ids)
    sessions = _linked_sessions(db, data_model_id, version_ids, schema_ids, database_ids)

    return ModelDeletion(
        data_model_id=data_model_id,
        name=model.name or "(unnamed)",
        versions=len(versions),
        schemas=schemas,
        databases=databases,
        datasets=datasets,
        correctives=correctives,
        queries_unlinked=len(queries),
        sessions_reset=len(sessions),
        _files=tuple(d.database_id for d in databases if d.file_exists),
    )


def delete_data_model(db: OrmSession, data_model_id: uuid.UUID) -> ModelDeletion | None:
    """Delete the model and everything derived from it. Returns what went.

    Order is the correctness argument, not a style choice. Every foreign key
    that points *into* the subtree from outside it is cleared before the subtree
    is deleted, so there is no window in which a row names something gone -- and
    the files go last-but-one, after the metadata is consistent and before the
    rows that name them disappear.
    """
    losses = plan(db, data_model_id)
    if losses is None:
        return None

    schema_ids = [s.schema_id for s in losses.schemas]
    database_ids = [d.database_id for d in losses.databases]
    version_ids = list(
        db.execute(
            select(DataModelVersion.id).where(DataModelVersion.data_model_id == data_model_id)
        ).scalars()
    )

    # 1. Outside pointers first. Queries keep their SQL and lose their link;
    #    everything else that names a doomed row is cleared outright.
    _unlink_queries(db, schema_ids, database_ids)
    _reset_sessions(db, data_model_id, version_ids, schema_ids, database_ids)
    _drop_pending_actions(db, data_model_id, database_ids)
    db.execute(sql_delete(Corrective).where(Corrective.data_model_id == data_model_id))
    db.execute(
        sql_delete(session_data_models).where(session_data_models.c.data_model_id == data_model_id)
    )
    db.flush()

    # 2. The files. `sample_db.destroy` is idempotent, so a database whose file
    #    was already gone is not a special case.
    for database_id in database_ids:
        sample_db.destroy(database_id)

    # 3. The subtree. The ORM relationships carry `cascade="all, delete-orphan"`
    #    from model → versions → schemas/datasets → databases, so this one
    #    delete removes all of it; the explicit work above is everything those
    #    cascades do not reach.
    model = db.get(DataModel, data_model_id)
    if model is not None:
        db.delete(model)
    db.flush()
    return losses


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------
def _tables_of(version: DataModelVersion) -> tuple[str, ...]:
    try:
        graph = EntityGraph.model_validate(version.graph)
    except ValueError:  # pragma: no cover - a stored graph that no longer parses
        return ()
    return tuple(table.name for table in graph.tables)


def _schemas_of(db: OrmSession, version_ids: list[uuid.UUID]) -> list[tuple[Schema, int]]:
    if not version_ids:
        return []
    rows = db.execute(
        select(Schema, DataModelVersion.version)
        .join(DataModelVersion, Schema.data_model_version_id == DataModelVersion.id)
        .where(Schema.data_model_version_id.in_(version_ids))
        .order_by(DataModelVersion.version, Schema.created_at)
    ).all()
    return [(row[0], int(row[1])) for row in rows]


def _databases_of(db: OrmSession, schema_ids: list[uuid.UUID]) -> list[tuple[Database, uuid.UUID]]:
    if not schema_ids:
        return []
    rows = db.execute(
        select(Database, Schema.data_model_version_id)
        .join(Schema, Database.schema_id == Schema.id)
        .where(Database.schema_id.in_(schema_ids))
        .order_by(Database.created_at)
    ).all()
    return [(row[0], row[1]) for row in rows]


def _database_loss(row: Database, tables: tuple[str, ...]) -> DatabaseLoss:
    exists = sample_db.exists(row.id)
    counts = sample_db.row_counts(row.id, tables) if exists else {}
    return DatabaseLoss(
        database_id=row.id,
        status=str(row.status),
        tables=tables,
        row_count=sum(counts.values()),
        file_exists=exists,
    )


def _linked_queries(
    db: OrmSession, schema_ids: list[uuid.UUID], database_ids: list[uuid.UUID]
) -> list[uuid.UUID]:
    if not schema_ids and not database_ids:
        return []
    clauses: list[ColumnElement[bool]] = []
    if schema_ids:
        clauses.append(Query.schema_id.in_(schema_ids))
    if database_ids:
        clauses.append(Query.database_id.in_(database_ids))
    stmt = select(Query.id).where(_any_of(clauses))
    return list(db.execute(stmt).scalars())


def _unlink_queries(
    db: OrmSession, schema_ids: list[uuid.UUID], database_ids: list[uuid.UUID]
) -> None:
    """Keep the SQL, drop the pointers. See the module docstring.

    ``synchronize_session="fetch"`` rather than ``False``: the factory builds
    sessions with ``expire_on_commit=False``, so a bulk update that skipped
    synchronisation would leave any already-loaded `Query` in this session still
    holding the id of a row that no longer exists. Cheaper to be right here than
    to make every caller remember to refresh.
    """
    if schema_ids:
        db.execute(
            update(Query)
            .where(Query.schema_id.in_(schema_ids))
            .values(schema_id=None)
            .execution_options(synchronize_session="fetch")
        )
    if database_ids:
        db.execute(
            update(Query)
            .where(Query.database_id.in_(database_ids))
            .values(database_id=None)
            .execution_options(synchronize_session="fetch")
        )


def _session_pointer_clauses(
    data_model_id: uuid.UUID,
    version_ids: list[uuid.UUID],
    schema_ids: list[uuid.UUID],
    database_ids: list[uuid.UUID],
) -> list[ColumnElement[bool]]:
    clauses: list[ColumnElement[bool]] = [SessionState.current_data_model_id == data_model_id]
    if version_ids:
        clauses.append(SessionState.current_data_model_version_id.in_(version_ids))
    if schema_ids:
        clauses.append(SessionState.current_schema_id.in_(schema_ids))
    if database_ids:
        clauses.append(SessionState.current_database_id.in_(database_ids))
    return clauses


def _linked_sessions(
    db: OrmSession,
    data_model_id: uuid.UUID,
    version_ids: list[uuid.UUID],
    schema_ids: list[uuid.UUID],
    database_ids: list[uuid.UUID],
) -> list[uuid.UUID]:
    clauses = _session_pointer_clauses(data_model_id, version_ids, schema_ids, database_ids)
    return list(db.execute(select(SessionState.session_id).where(_any_of(clauses))).scalars())


def _reset_sessions(
    db: OrmSession,
    data_model_id: uuid.UUID,
    version_ids: list[uuid.UUID],
    schema_ids: list[uuid.UUID],
    database_ids: list[uuid.UUID],
) -> None:
    """Clear every "current" pointer naming the subtree, in EVERY session.

    Not only the session doing the deleting. Another conversation in the same
    project can be sitting on this model, and a pointer left behind there is
    both a dangling foreign key and a conversation that would answer "where am
    I?" with a row that no longer exists.
    """
    clauses = _session_pointer_clauses(data_model_id, version_ids, schema_ids, database_ids)
    for row in db.execute(select(SessionState).where(_any_of(clauses))).scalars():
        if row.current_data_model_id == data_model_id:
            row.current_data_model_id = None
        if version_ids and row.current_data_model_version_id in version_ids:
            row.current_data_model_version_id = None
        if schema_ids and row.current_schema_id in schema_ids:
            row.current_schema_id = None
        if database_ids and row.current_database_id in database_ids:
            row.current_database_id = None


def _drop_pending_actions(
    db: OrmSession, data_model_id: uuid.UUID, database_ids: list[uuid.UUID]
) -> None:
    """A permission to destroy something that is about to not exist is not a permission.

    Any session holding one of these would otherwise carry a live "yes" against
    a deleted row, which is a dangling foreign key *and* a confirmation whose
    subject changed underneath it.
    """
    clauses: list[ColumnElement[bool]] = [PendingAction.data_model_id == data_model_id]
    if database_ids:
        clauses.append(PendingAction.database_id.in_(database_ids))
    db.execute(sql_delete(PendingAction).where(_any_of(clauses)))


def _any_of(clauses: list[ColumnElement[bool]]) -> ColumnElement[bool]:
    """``OR`` over a non-empty clause list; a single clause stays itself."""
    return clauses[0] if len(clauses) == 1 else or_(*clauses)
