"""Default project/session bootstrap (MAIN.md clarification 2).

"There will always be a default 'project' and 'session' for every user, so
they are optional parameters and not necessarily part of the URL schema."
This package has no `User` entity (auth/multi-tenancy is explicitly out of
scope, D2) -- so "default" here means one well-known singleton project (and
one default session within it), addressed by a fixed slug rather than a
per-user identifier. Every repository function that takes an optional
`project_id`/`session_id` falls back to these.

Idempotent by construction: `slug` carries a unique constraint (see
`foundation.models.Project.slug`, `Session.__table_args__`), so a second
caller racing the first either sees the row the first one just committed, or
hits the `IntegrityError` from the unique constraint and re-queries -- never
a duplicate default.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from foundation.models import Project
from foundation.models import Session as SessionModel

DEFAULT_PROJECT_SLUG = "default"
DEFAULT_SESSION_SLUG = "default"


def get_or_create_default_project(db: OrmSession) -> Project:
    stmt = select(Project).where(Project.slug == DEFAULT_PROJECT_SLUG)
    existing = db.execute(stmt).scalar_one_or_none()
    if existing is not None:
        return existing

    project = Project(name="Default Project", slug=DEFAULT_PROJECT_SLUG, is_default=True)
    db.add(project)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return db.execute(select(Project).where(Project.slug == DEFAULT_PROJECT_SLUG)).scalar_one()
    return project


def get_or_create_default_session(db: OrmSession, project: Project | None = None) -> SessionModel:
    project = project or get_or_create_default_project(db)

    existing = db.execute(
        select(SessionModel).where(
            SessionModel.project_id == project.id,
            SessionModel.slug == DEFAULT_SESSION_SLUG,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    session_row = SessionModel(project_id=project.id, slug=DEFAULT_SESSION_SLUG, is_default=True)
    db.add(session_row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return db.execute(
            select(SessionModel).where(
                SessionModel.project_id == project.id,
                SessionModel.slug == DEFAULT_SESSION_SLUG,
            )
        ).scalar_one()
    return session_row
