"""The FastAPI application (design §5).

`create_app` is the one place all the wiring decisions in this package come
together: which SQLAlchemy engine backs layer 1, which `InferenceClient`
backs layer 2, and which validators the repair loop uses. Every argument
defaults to something that works for local development against a live
Fireworks account, but every argument can also be swapped for an offline
double -- most importantly `InferenceClient`, which is how the whole API
becomes testable without a network (D8). See `t2s_api.deps` for how each
request gets whatever was wired here.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Engine

from foundation.db import init_db, make_session_factory
from t2s_api.db import default_engine
from t2s_api.problems import register_exception_handlers
from t2s_api.routers import (
    data_models,
    databases,
    datasets,
    projects,
    schemas_router,
    sessions,
    text_to_sql,
)
from t2s_core.ports import InferenceClient, QueryValidator, SchemaValidator

__all__ = ["app", "create_app"]


#: Browser origins allowed to call this API. Defaults to the usual local dev
#: servers so a Vue/Vite UI works out of the box; override with a
#: comma-separated ``T2S_CORS_ORIGINS``. Deliberately NOT ``*``: this API can
#: create and query databases, and a wildcard default is the kind of thing that
#: survives all the way to a deployment nobody re-read.
DEFAULT_CORS_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)


def _cors_origins() -> list[str]:
    configured = os.environ.get("T2S_CORS_ORIGINS")
    if not configured:
        return list(DEFAULT_CORS_ORIGINS)
    return [origin.strip() for origin in configured.split(",") if origin.strip()]


def _install_cors(app: FastAPI) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )


def create_app(
    *,
    inference_client: InferenceClient | None = None,
    engine: Engine | None = None,
    query_validator: QueryValidator | None = None,
    schema_validator: SchemaValidator | None = None,
) -> FastAPI:
    """Build the app.

    ``inference_client=None`` (the default) defers building a live
    ``FireworksClient`` until the first layer-2 request actually needs one
    (see `t2s_api.deps.get_inference_client`) -- so importing/starting the
    app, and every layer-1 CRUD request, never requires ``FIREWORKS_API_KEY``
    to be set. Pass a `t2s_core.clients.RecordedClient` (or any other
    `InferenceClient`) to run the whole app offline, which is exactly what
    the test suite does (D8).
    """
    app = FastAPI(
        title="text-to-sql API",
        version="0.1.0",
        description=(
            "Layer 1: deterministic CRUD over projects, sessions, data models, schemas, "
            "datasets, and sample databases. Layer 2: stateless text-to-SQL generation. "
            "See memory/design.md §5."
        ),
    )

    _install_cors(app)

    resolved_engine = engine or default_engine()
    init_db(resolved_engine)
    app.state.session_factory = make_session_factory(resolved_engine)
    app.state.inference_client = inference_client
    app.state.query_validator = query_validator
    app.state.schema_validator = schema_validator

    app.include_router(projects.router)
    app.include_router(sessions.router)
    app.include_router(data_models.router)
    app.include_router(schemas_router.router)
    app.include_router(datasets.router)
    app.include_router(databases.router)
    app.include_router(text_to_sql.router)

    register_exception_handlers(app)
    return app


#: Module-level instance for ``uvicorn t2s_api.app:app`` (see the Makefile's
#: ``serve`` target). Built with every default: an in-memory metadata store
#: (override with ``T2S_API_DB_URL`` for persistence) and a lazily-constructed
#: live ``FireworksClient`` for layer 2 (requires ``FIREWORKS_API_KEY``).
app = create_app()
