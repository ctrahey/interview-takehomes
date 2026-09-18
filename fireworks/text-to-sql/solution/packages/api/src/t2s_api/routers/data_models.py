"""Layer 1: data models and their versioned entity graphs (design §4/§5, D6)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from foundation.repositories import DataModelRepository, DataModelVersionRepository
from t2s_api.deps import DbSession
from t2s_api.problems import ApiProblem
from t2s_api.schemas import (
    DataModelCreate,
    DataModelOut,
    DataModelVersionCreate,
    DataModelVersionOut,
)

router = APIRouter(tags=["data-models"])


@router.post("/data-models", response_model=DataModelOut, status_code=201)
def create_data_model(body: DataModelCreate, db: DbSession) -> DataModelOut:
    model = DataModelRepository(db).create(project_id=body.project_id, name=body.name)
    return DataModelOut.model_validate(model)


@router.get("/data-models", response_model=list[DataModelOut])
def list_data_models(db: DbSession, project_id: uuid.UUID | None = None) -> list[DataModelOut]:
    return [
        DataModelOut.model_validate(m) for m in DataModelRepository(db).list(project_id=project_id)
    ]


@router.get("/data-models/{data_model_id}", response_model=DataModelOut)
def get_data_model(data_model_id: uuid.UUID, db: DbSession) -> DataModelOut:
    model = DataModelRepository(db).get(data_model_id)
    if model is None:
        raise ApiProblem(
            404, "not-found", "Data Model Not Found", f"no data model with id {data_model_id}"
        )
    return DataModelOut.model_validate(model)


@router.post(
    "/data-models/{data_model_id}/versions", response_model=DataModelVersionOut, status_code=201
)
def create_data_model_version(
    data_model_id: uuid.UUID, body: DataModelVersionCreate, db: DbSession
) -> DataModelVersionOut:
    if DataModelRepository(db).get(data_model_id) is None:
        raise ApiProblem(
            404, "not-found", "Data Model Not Found", f"no data model with id {data_model_id}"
        )
    version = DataModelVersionRepository(db).create(data_model_id, body.graph)
    return DataModelVersionOut.model_validate(version)


@router.get("/data-models/{data_model_id}/versions/latest", response_model=DataModelVersionOut)
def get_latest_data_model_version(data_model_id: uuid.UUID, db: DbSession) -> DataModelVersionOut:
    version = DataModelVersionRepository(db).latest(data_model_id)
    if version is None:
        raise ApiProblem(
            404,
            "not-found",
            "No Versions",
            f"data model {data_model_id} has no versions yet",
        )
    return DataModelVersionOut.model_validate(version)


@router.get("/model-versions/{version_id}", response_model=DataModelVersionOut)
def get_data_model_version(version_id: uuid.UUID, db: DbSession) -> DataModelVersionOut:
    version = DataModelVersionRepository(db).get(version_id)
    if version is None:
        raise ApiProblem(
            404, "not-found", "Model Version Not Found", f"no model version with id {version_id}"
        )
    return DataModelVersionOut.model_validate(version)
