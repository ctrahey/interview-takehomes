"""Layer 1: datasets -- importable sample data for one data model version (design §4)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from foundation.repositories import DataModelVersionRepository, DatasetRepository
from t2s_api.deps import DbSession
from t2s_api.problems import ApiProblem
from t2s_api.schemas import DatasetCreate, DatasetOut

router = APIRouter(tags=["datasets"])


@router.post("/model-versions/{version_id}/datasets", response_model=DatasetOut, status_code=201)
def create_dataset(version_id: uuid.UUID, body: DatasetCreate, db: DbSession) -> DatasetOut:
    if DataModelVersionRepository(db).get(version_id) is None:
        raise ApiProblem(
            404, "not-found", "Model Version Not Found", f"no model version with id {version_id}"
        )
    row = DatasetRepository(db).create(version_id, body.name, body.rows)
    return DatasetOut.model_validate(row)


@router.get("/datasets/{dataset_id}", response_model=DatasetOut)
def get_dataset(dataset_id: uuid.UUID, db: DbSession) -> DatasetOut:
    row = DatasetRepository(db).get(dataset_id)
    if row is None:
        raise ApiProblem(404, "not-found", "Dataset Not Found", f"no dataset with id {dataset_id}")
    return DatasetOut.model_validate(row)
