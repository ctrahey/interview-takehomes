"""Deterministic answers about the user's own objects. **No LLM reaches here.**

This module is the reason the three-layer split exists, so it is worth stating
the rule in the one place it is enforced:

    The LLM classifies and generates. It NEVER reports system state.

When the user says "show me my databases", an LLM decides that the utterance is
an ``inspect`` intent with target ``databases`` -- and that is the whole of its
involvement. The list of databases, their ids, their statuses and their row
counts are read out of ``foundation`` by the functions below and rendered by our
own code. A model is never in a position to invent a database name, a row count,
or a column type, because it is never asked and its output is never consulted
for these answers.

Every function here takes a SQLAlchemy session and returns a ``DataTable``.
None of them takes an ``InferenceClient``; none of them imports one. That is not
an accident and ``tests/test_state_is_deterministic.py`` asserts it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from foundation import sample_db
from foundation.graph import EntityGraph
from foundation.models import (
    Corrective,
    Database,
    DataModel,
    DataModelVersion,
    Query,
    Schema,
    SessionState,
)
from foundation.models import Session as SessionModel
from foundation.repositories import (
    CorrectiveRepository,
    DataModelVersionRepository,
    SessionStateRepository,
)
from t2s_nl.turns import DataTable

__all__ = [
    "SAMPLE_ROW_CAP",
    "list_correctives",
    "list_databases",
    "list_models",
    "list_queries",
    "list_schemas",
    "list_sessions",
    "sample_rows",
    "schema_detail",
    "state_summary",
]

SAMPLE_ROW_CAP = 20


def _short(value: uuid.UUID | None) -> str:
    return "-" if value is None else str(value)[:8]


def _stamp(row: object) -> str:
    created = getattr(row, "created_at", None)
    return created.strftime("%Y-%m-%d %H:%M") if created is not None else "-"


# ---------------------------------------------------------------------------
# Catalogue reads
# ---------------------------------------------------------------------------
def list_models(db: OrmSession, project_id: uuid.UUID) -> DataTable:
    models = list(
        db.execute(
            select(DataModel)
            .where(DataModel.project_id == project_id)
            .order_by(DataModel.created_at)
        ).scalars()
    )
    rows: list[list[str]] = []
    for model in models:
        versions = model.versions
        latest = versions[-1] if versions else None
        table_count = len(EntityGraph.model_validate(latest.graph).tables) if latest else 0
        rows.append(
            [
                _short(model.id),
                model.name or "(unnamed)",
                str(latest.version) if latest else "0",
                str(table_count),
                _stamp(model),
            ]
        )
    return DataTable(
        columns=["id", "name", "versions", "tables", "created"],
        rows=rows,
        caption="data models" if rows else "no data models yet",
    )


def list_schemas(db: OrmSession, project_id: uuid.UUID) -> DataTable:
    stmt = (
        select(Schema, DataModel, DataModelVersion)
        .join(DataModelVersion, Schema.data_model_version_id == DataModelVersion.id)
        .join(DataModel, DataModelVersion.data_model_id == DataModel.id)
        .where(DataModel.project_id == project_id)
        .order_by(Schema.created_at)
    )
    rows = [
        [
            _short(schema.id),
            model.name or "(unnamed)",
            f"v{version.version}",
            schema.dialect,
            f"{len(schema.ddl.splitlines())} lines",
            _stamp(schema),
        ]
        for schema, model, version in db.execute(stmt).all()
    ]
    return DataTable(
        columns=["id", "model", "version", "dialect", "ddl", "created"],
        rows=rows,
        caption="schemas" if rows else "no schemas yet",
    )


def list_databases(db: OrmSession, project_id: uuid.UUID) -> DataTable:
    stmt = (
        select(Database, DataModel, DataModelVersion)
        .join(Schema, Database.schema_id == Schema.id)
        .join(DataModelVersion, Schema.data_model_version_id == DataModelVersion.id)
        .join(DataModel, DataModelVersion.data_model_id == DataModel.id)
        .where(DataModel.project_id == project_id)
        .order_by(Database.created_at)
    )
    rows = []
    for database, model, version in db.execute(stmt).all():
        rows.append(
            [
                _short(database.id),
                model.name or "(unnamed)",
                f"v{version.version}",
                database.engine,
                str(database.status),
                "yes" if sample_db.exists(database.id) else "no",
                _stamp(database),
            ]
        )
    return DataTable(
        columns=["id", "model", "version", "engine", "status", "on disk", "created"],
        rows=rows,
        caption="sample databases" if rows else "no sample databases yet",
    )


def list_queries(db: OrmSession, session_id: uuid.UUID, *, limit: int = 10) -> DataTable:
    queries = list(
        db.execute(
            select(Query)
            .where(Query.session_id == session_id)
            .order_by(Query.created_at.desc())
            .limit(limit)
        ).scalars()
    )
    rows = [
        [
            _short(q.id),
            (q.question or "-")[:48],
            q.response_class or "-",
            " ".join(q.sql.split())[:60],
            _stamp(q),
        ]
        for q in reversed(queries)
    ]
    return DataTable(
        columns=["id", "question", "class", "sql", "created"],
        rows=rows,
        caption="queries in this session" if rows else "no queries in this session yet",
    )


def list_sessions(db: OrmSession, project_id: uuid.UUID) -> DataTable:
    sessions = list(
        db.execute(
            select(SessionModel)
            .where(SessionModel.project_id == project_id)
            .order_by(SessionModel.created_at)
        ).scalars()
    )
    rows = [[_short(s.id), s.slug or "(unnamed)", str(len(s.queries)), _stamp(s)] for s in sessions]
    return DataTable(columns=["id", "slug", "queries", "created"], rows=rows, caption="sessions")


def list_correctives(db: OrmSession, data_model_id: uuid.UUID | None) -> DataTable:
    if data_model_id is None:
        return DataTable(columns=["#", "corrective"], rows=[], caption="no current data model")
    items: Sequence[Corrective] = CorrectiveRepository(db).list_active(data_model_id)
    rows = [[str(i), c.text] for i, c in enumerate(items, start=1)]
    return DataTable(
        columns=["#", "corrective"],
        rows=rows,
        caption="correctives on the current model" if rows else "no correctives on this model",
    )


# ---------------------------------------------------------------------------
# Schema structure -- read from the stored entity graph, not from the model
# ---------------------------------------------------------------------------
def schema_detail(
    db: OrmSession, version_id: uuid.UUID | None, *, table: str | None = None
) -> DataTable:
    """Columns of the current data model version, optionally one table only.

    Note what this is *not*: it is not ``SELECT sql FROM sqlite_master``. D12
    denies catalog access on the sample-database path precisely so that "what
    columns does X have" is answered from the stored entity graph -- the
    authoritative, dialect-neutral artefact -- rather than by interrogating a
    live database. This function is the deterministic answer D12's refusal
    message points at.
    """
    if version_id is None:
        return DataTable(
            columns=["table", "column", "type", "null", "key"],
            rows=[],
            caption="no current schema",
        )
    graph = DataModelVersionRepository(db).get_graph(version_id)
    wanted = table.lower() if table else None
    rows: list[list[str]] = []
    matched = False
    for tbl in graph.tables:
        if wanted is not None and tbl.name.lower() != wanted:
            continue
        matched = True
        fk_columns = {c: fk.ref_table for fk in tbl.foreign_keys for c in fk.columns}
        for col in tbl.columns:
            key = ""
            if col.name in tbl.primary_key:
                key = "PK"
            if col.name in fk_columns:
                key = f"{key + ' ' if key else ''}FK→{fk_columns[col.name]}"
            rows.append(
                [
                    tbl.name,
                    col.name,
                    col.type,
                    "" if col.nullable else "NOT NULL",
                    key,
                ]
            )
    if wanted is not None and not matched:
        names = ", ".join(t.name for t in graph.tables)
        return DataTable(
            columns=["table", "column", "type", "null", "key"],
            rows=[],
            caption=f"no table named {table!r} in this schema. Tables: {names}",
        )
    return DataTable(
        columns=["table", "column", "type", "null", "key"],
        rows=rows,
        caption=f"{len(graph.tables)} table(s)",
    )


# ---------------------------------------------------------------------------
# Sample rows -- a gated read against the user's own sample database
# ---------------------------------------------------------------------------
def sample_rows(
    db: OrmSession,
    database_id: uuid.UUID | None,
    version_id: uuid.UUID | None,
    table: str | None,
    *,
    limit: int = SAMPLE_ROW_CAP,
) -> DataTable:
    """``SELECT * FROM <table> LIMIT n`` -- with the table name validated first.

    The SQL here is composed by us, from an identifier checked against the
    stored entity graph. No model output is interpolated, so there is nothing
    for an injection to ride in on; and it still goes through
    ``foundation.sample_db.query``, which re-applies the D9 gate, the read-only
    connection, the timeout and the row cap.
    """
    if database_id is None:
        return DataTable(columns=[], rows=[], caption="no sample database is loaded")
    if version_id is None:
        return DataTable(columns=[], rows=[], caption="no current schema")
    graph = DataModelVersionRepository(db).get_graph(version_id)
    names = [t.name for t in graph.tables]
    if table is None:
        if not names:
            return DataTable(columns=[], rows=[], caption="the current schema has no tables")
        return DataTable(
            columns=[],
            rows=[],
            caption="which table? this schema has: " + ", ".join(names),
        )
    match = next((n for n in names if n.lower() == table.lower()), None)
    if match is None:
        return DataTable(
            columns=[],
            rows=[],
            caption=f"no table named {table!r} in this schema. Tables: " + ", ".join(names),
        )

    result = sample_db.query(
        database_id, f'SELECT * FROM "{match}" LIMIT {int(limit) + 1}', row_cap=limit + 1
    )
    truncated = result.row_count > limit
    rows = [[_cell(v) for v in row] for row in result.rows[:limit]]
    return DataTable(
        columns=list(result.columns),
        rows=rows,
        caption=f"rows from {match}",
        truncated=truncated,
    )


def _cell(value: object) -> str:
    return "NULL" if value is None else str(value)


# ---------------------------------------------------------------------------
# "Where am I?"
# ---------------------------------------------------------------------------
def state_summary(db: OrmSession, session_id: uuid.UUID) -> DataTable:
    state: SessionState = SessionStateRepository(db).get_or_create(session_id)
    model = (
        db.get(DataModel, state.current_data_model_id)
        if state.current_data_model_id is not None
        else None
    )
    version = (
        db.get(DataModelVersion, state.current_data_model_version_id)
        if state.current_data_model_version_id is not None
        else None
    )
    schema = (
        db.get(Schema, state.current_schema_id) if state.current_schema_id is not None else None
    )
    database = (
        db.get(Database, state.current_database_id)
        if state.current_database_id is not None
        else None
    )
    last_query = db.get(Query, state.last_query_id) if state.last_query_id is not None else None
    correctives = (
        CorrectiveRepository(db).list_active(state.current_data_model_id)
        if state.current_data_model_id is not None
        else []
    )
    rows = [
        ["session", str(session_id)],
        ["data model", f"{model.name or '(unnamed)'} [{_short(model.id)}]" if model else "-"],
        ["version", f"v{version.version}" if version else "-"],
        ["schema", f"{schema.dialect} [{_short(schema.id)}]" if schema else "-"],
        [
            "sample database",
            f"{database.status} [{_short(database.id)}]" if database else "-",
        ],
        ["last question", state.last_question or "-"],
        ["last query", " ".join(last_query.sql.split())[:70] if last_query else "-"],
        ["correctives", str(len(correctives))],
    ]
    return DataTable(columns=["", ""], rows=rows, caption="current session state")
