"""Layer 1: schemas -- concrete DDL projections of a data model version (D6).

Deterministic: `render_ddl` is `graph -> DDL`, a pure function, never a model
call. This is also the endpoint the D12 catalog-denial refusal points users
at for "what tables/columns exist" -- `GET /schemas/{schema_id}` answers that
exactly, with no reconnaissance risk.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from foundation.ddl import render_ddl
from foundation.repositories import DataModelVersionRepository, SchemaRepository
from t2s_api.deps import DbSession
from t2s_api.problems import ApiProblem
from t2s_api.schemas import SchemaCreate, SchemaOut

router = APIRouter(tags=["schemas"])


@router.post("/model-versions/{version_id}/schemas", response_model=SchemaOut, status_code=201)
def create_schema(version_id: uuid.UUID, body: SchemaCreate, db: DbSession) -> SchemaOut:
    version_repo = DataModelVersionRepository(db)
    graph = version_repo.get_graph(version_id)  # raises ValueError -> 400 if missing
    ddl_text = render_ddl(graph, body.dialect)
    row = SchemaRepository(db).create(version_id, body.dialect, ddl_text)
    return SchemaOut.model_validate(row)


@router.get("/model-versions/{version_id}/schemas", response_model=list[SchemaOut])
def list_schemas_for_version(version_id: uuid.UUID, db: DbSession) -> list[SchemaOut]:
    return [SchemaOut.model_validate(s) for s in SchemaRepository(db).list_for_version(version_id)]


@router.get("/schemas/{schema_id}", response_model=SchemaOut)
def get_schema(schema_id: uuid.UUID, db: DbSession) -> SchemaOut:
    row = SchemaRepository(db).get(schema_id)
    if row is None:
        raise ApiProblem(404, "not-found", "Schema Not Found", f"no schema with id {schema_id}")
    return SchemaOut.model_validate(row)
