"""Layer 1: projects (design §5) -- deterministic CRUD, zero natural language."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from foundation.repositories import ProjectRepository
from t2s_api.deps import DbSession
from t2s_api.problems import ApiProblem
from t2s_api.schemas import ProjectCreate, ProjectOut

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectCreate, db: DbSession) -> ProjectOut:
    project = ProjectRepository(db).create(name=body.name, slug=body.slug)
    return ProjectOut.model_validate(project)


@router.get("", response_model=list[ProjectOut])
def list_projects(db: DbSession) -> list[ProjectOut]:
    return [ProjectOut.model_validate(p) for p in ProjectRepository(db).list()]


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: uuid.UUID, db: DbSession) -> ProjectOut:
    project = ProjectRepository(db).get(project_id)
    if project is None:
        raise ApiProblem(404, "not-found", "Project Not Found", f"no project with id {project_id}")
    return ProjectOut.model_validate(project)
