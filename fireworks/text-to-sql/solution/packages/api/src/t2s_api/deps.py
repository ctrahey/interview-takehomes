"""Dependency injection (design §5).

The load-bearing property this module exists to guarantee: the app must be
constructible entirely with a `RecordedClient` so the whole API is testable
offline (D8), and that is a wiring concern owned here and in
`t2s_api.app.create_app` -- never a monkeypatch reached for inside a test
fixture. `create_app` accepts an `InferenceClient` (and optional validators)
directly; these dependency functions just hand whatever was wired at
app-construction time to each request.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session as OrmSession

from t2s_api.problems import ApiProblem
from t2s_core.clients import FireworksClient
from t2s_core.config import FireworksConfig
from t2s_core.ports import InferenceClient, QueryValidator, SchemaValidator

__all__ = [
    "DbSession",
    "InjectedInferenceClient",
    "InjectedQueryValidator",
    "InjectedSchemaValidator",
    "get_db",
    "get_inference_client",
    "get_query_validator",
    "get_schema_validator",
]


def get_db(request: Request) -> Iterator[OrmSession]:
    """One transactional session per request: commits on a clean response, rolls back
    on any exception (mirrors `foundation.db.session_scope`)."""
    factory = request.app.state.session_factory
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_inference_client(request: Request) -> InferenceClient:
    """Whatever `create_app(inference_client=...)` was given.

    Only constructs a live `FireworksClient` -- reading `FIREWORKS_API_KEY`
    from the environment -- lazily, and only when nothing was injected. Tests
    and CI always inject a `RecordedClient` (or another offline double)
    explicitly, so this branch is never exercised offline (D8).
    """
    client: InferenceClient | None = request.app.state.inference_client
    if client is None:
        try:
            client = FireworksClient(FireworksConfig.from_env())
        except RuntimeError as exc:
            # A missing credential is a deployment condition, not a bug, and it
            # must not read as "something broke inside the server". 503 with a
            # concrete remedy: the operator sets a variable, the caller retries.
            # Layer-1 CRUD keeps working throughout -- that is the whole reason
            # this client is built lazily rather than at startup.
            raise ApiProblem(
                status_code=503,
                type_slug="inference-unavailable",
                title="Inference Unavailable",
                detail=(
                    "This endpoint needs a language model, and no Fireworks "
                    "credential is configured. Set FIREWORKS_API_KEY on the "
                    "server. Endpoints that do not call a model are unaffected."
                ),
            ) from exc
        request.app.state.inference_client = client
    return client


def get_query_validator(request: Request) -> QueryValidator | None:
    """`None` lets `t2s_core.generate_query` fall back to its own per-dialect default."""
    validator: QueryValidator | None = request.app.state.query_validator
    return validator


def get_schema_validator(request: Request) -> SchemaValidator | None:
    validator: SchemaValidator | None = request.app.state.schema_validator
    return validator


# `Annotated[..., Depends(...)]` (rather than `= Depends(...)` as a default
# value) keeps every route signature free of B008-style "function call in a
# default argument" lint noise -- it is also the FastAPI-recommended style.
DbSession = Annotated[OrmSession, Depends(get_db)]
InjectedInferenceClient = Annotated[InferenceClient, Depends(get_inference_client)]
InjectedQueryValidator = Annotated[QueryValidator | None, Depends(get_query_validator)]
InjectedSchemaValidator = Annotated[SchemaValidator | None, Depends(get_schema_validator)]
