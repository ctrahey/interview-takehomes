"""Layer 1: the sample database lifecycle -- create -> load -> query -> destroy (D9, D12).

`query_database` is the security-critical endpoint: every candidate SQL
statement runs through `foundation.sample_db.query`, which enforces the D9
AST safety gate (single read-only SELECT/WITH, no DDL/DML) and, per D12,
denies `sqlite_master`/`sqlite_schema` catalog access unless the request
explicitly opts in with `allow_catalog: true`. Both rejections surface as
RFC 9457 problem responses (`t2s_api.problems`), not silent empty results.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy.orm import Session as OrmSession

from foundation import sample_db
from foundation.repositories import DatabaseRepository, DatasetRepository, SchemaRepository
from t2s_api.deps import DbSession
from t2s_api.problems import ApiProblem
from t2s_api.schemas import (
    DatabaseCreate,
    DatabaseLoadRequest,
    DatabaseLoadResponse,
    DatabaseOut,
    DatabaseQueryRequest,
    DatabaseQueryResponse,
)

router = APIRouter(prefix="/databases", tags=["databases"])


def _require_database(database_id: uuid.UUID, db: OrmSession) -> None:
    if DatabaseRepository(db).get(database_id) is None:
        raise ApiProblem(
            404,
            "database-not-found",
            "Database Not Found",
            f"no database with id {database_id}",
        )


@router.post("", response_model=DatabaseOut, status_code=201)
def create_database(body: DatabaseCreate, db: DbSession) -> DatabaseOut:
    schema_row = SchemaRepository(db).get(body.schema_id)
    if schema_row is None:
        raise ApiProblem(
            404, "not-found", "Schema Not Found", f"no schema with id {body.schema_id}"
        )
    database_id = sample_db.create(schema_row.ddl, dialect=schema_row.dialect)
    row = DatabaseRepository(db).create(schema_row.id, database_id, engine=schema_row.dialect)
    return DatabaseOut.model_validate(row)


@router.get("/{database_id}", response_model=DatabaseOut)
def get_database(database_id: uuid.UUID, db: DbSession) -> DatabaseOut:
    row = DatabaseRepository(db).get(database_id)
    if row is None:
        raise ApiProblem(
            404,
            "database-not-found",
            "Database Not Found",
            f"no database with id {database_id}",
        )
    return DatabaseOut.model_validate(row)


@router.post("/{database_id}/load", response_model=DatabaseLoadResponse)
def load_database(
    database_id: uuid.UUID, body: DatabaseLoadRequest, db: DbSession
) -> DatabaseLoadResponse:
    _require_database(database_id, db)
    dataset = DatasetRepository(db).get(body.dataset_id)
    if dataset is None:
        raise ApiProblem(
            404, "not-found", "Dataset Not Found", f"no dataset with id {body.dataset_id}"
        )
    inserted = sample_db.load(database_id, dataset.rows)
    DatabaseRepository(db).mark_loaded(database_id)
    return DatabaseLoadResponse(rows_inserted=inserted)


@router.post("/{database_id}/query", response_model=DatabaseQueryResponse)
def query_database(
    database_id: uuid.UUID, body: DatabaseQueryRequest, db: DbSession
) -> DatabaseQueryResponse:
    _require_database(database_id, db)
    result = sample_db.query(database_id, body.sql, allow_catalog=body.allow_catalog)
    return DatabaseQueryResponse(
        columns=result.columns,
        rows=[list(r) for r in result.rows],
        row_count=result.row_count,
        truncated=result.truncated,
    )


@router.delete("/{database_id}", status_code=204, response_model=None)
def destroy_database(database_id: uuid.UUID, db: DbSession) -> None:
    _require_database(database_id, db)
    sample_db.destroy(database_id)
    DatabaseRepository(db).mark_destroyed(database_id)
