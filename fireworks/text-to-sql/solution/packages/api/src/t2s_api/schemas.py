"""Request/response models for the layer-1 CRUD surface (design §5).

Thin pydantic wrappers around `foundation.models` rows -- never the ORM rows
themselves over the wire. `*Out` models read straight off the ORM object via
`from_attributes=True`; `*Create` models are the request bodies.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from foundation.graph import EntityGraph

__all__ = [
    "DatabaseCreate",
    "DatabaseLoadRequest",
    "DatabaseLoadResponse",
    "DatabaseOut",
    "DatabaseQueryRequest",
    "DatabaseQueryResponse",
    "DataModelCreate",
    "DataModelOut",
    "DataModelVersionCreate",
    "DataModelVersionOut",
    "DatasetCreate",
    "DatasetOut",
    "ProjectCreate",
    "ProjectOut",
    "SchemaCreate",
    "SchemaOut",
    "SessionAttachDataModel",
    "SessionCreate",
    "SessionOut",
]


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    slug: str = Field(min_length=1)


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    slug: str
    is_default: bool
    created_at: datetime
    updated_at: datetime


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: uuid.UUID | None = None
    slug: str | None = None
    summary: str | None = None


class SessionAttachDataModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_model_id: uuid.UUID


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    project_id: uuid.UUID
    slug: str | None
    is_default: bool
    summary: str | None
    created_at: datetime
    updated_at: datetime


class DataModelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: uuid.UUID | None = None
    name: str | None = None


class DataModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    project_id: uuid.UUID
    name: str | None
    created_at: datetime
    updated_at: datetime


class DataModelVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    graph: EntityGraph


class DataModelVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    data_model_id: uuid.UUID
    version: int
    graph: dict[str, Any]
    graph_schema_version: int
    created_at: datetime


class SchemaCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dialect: str = "sqlite"


class SchemaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    data_model_version_id: uuid.UUID
    dialect: str
    ddl: str
    created_at: datetime


class DatasetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    rows: dict[str, list[dict[str, Any]]]


class DatasetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    data_model_version_id: uuid.UUID
    name: str
    rows: dict[str, list[dict[str, Any]]]
    created_at: datetime


class DatabaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_id: uuid.UUID


class DatabaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    schema_id: uuid.UUID
    engine: str
    status: str
    created_at: datetime
    destroyed_at: datetime | None


class DatabaseLoadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: uuid.UUID


class DatabaseLoadResponse(BaseModel):
    rows_inserted: int


class DatabaseQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str = Field(min_length=1)
    #: D12 opt-in. Default-closed: a request that doesn't mention it gets the
    #: safe behaviour.
    allow_catalog: bool = False


class DatabaseQueryResponse(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
