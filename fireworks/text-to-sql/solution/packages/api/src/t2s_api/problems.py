"""RFC 9457 `application/problem+json` error responses (design §5).

One exception type per failure family, one handler each, registered on the
app in `register_exception_handlers`. Two rules this module exists to
enforce:

* HTTP errors are problem documents -- never FastAPI's default `{"detail":
  ...}` shape, and never a bare 500 with a traceback leaking to the client.
* A Fireworks upstream failure never puts the provider's raw error body (or,
  obviously, the API key -- which never reaches this layer as anything but a
  `SecretStr` to begin with, per `t2s_core.config`) into a response or a log
  line here. Every t2s_core `InferenceError` handler below writes its own
  fixed, generic sentence; only the opaque `request_id` (a support handle,
  not a secret) is echoed back.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from foundation.sample_db import (
    DatabaseNotFoundError,
    InvalidIdentifierError,
    QueryTimeoutError,
    SampleDatabaseError,
)
from foundation.security import CatalogAccessDeniedError, UnsafeQueryError
from t2s_core.errors import (
    FixtureNotFound,
    InvalidResponse,
    RateLimited,
    T2SError,
    TransportError,
    TruncatedResponse,
    UpstreamError,
)

__all__ = ["ApiProblem", "register_exception_handlers"]

PROBLEM_TYPE_BASE = "https://t2s.dev/problems/"
PROBLEM_MEDIA_TYPE = "application/problem+json"


class ApiProblem(Exception):
    """Raise this anywhere in a router to produce a problem+json response."""

    def __init__(
        self, status_code: int, type_slug: str, title: str, detail: str, **extra: Any
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.type_slug = type_slug
        self.title = title
        self.detail = detail
        self.extra = extra


def _problem(
    request: Request,
    *,
    status_code: int,
    type_slug: str,
    title: str,
    detail: str,
    **extra: Any,
) -> JSONResponse:
    body = {
        "type": PROBLEM_TYPE_BASE + type_slug,
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": request.url.path,
        **{k: v for k, v in extra.items() if v is not None},
    }
    return JSONResponse(
        jsonable_encoder(body), status_code=status_code, media_type=PROBLEM_MEDIA_TYPE
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiProblem)
    def _handle_api_problem(request: Request, exc: ApiProblem) -> JSONResponse:
        return _problem(
            request,
            status_code=exc.status_code,
            type_slug=exc.type_slug,
            title=exc.title,
            detail=exc.detail,
            **exc.extra,
        )

    @app.exception_handler(RequestValidationError)
    def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _problem(
            request,
            status_code=422,
            type_slug="validation-error",
            title="Validation Error",
            detail="The request did not match the expected shape.",
            errors=jsonable_encoder(exc.errors()),
        )

    @app.exception_handler(StarletteHTTPException)
    def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _problem(
            request,
            status_code=exc.status_code,
            type_slug="http-error",
            title="HTTP Error",
            detail=str(exc.detail),
        )

    @app.exception_handler(ValueError)
    def _handle_value_error(request: Request, exc: ValueError) -> JSONResponse:
        # Several `foundation.repositories` methods raise a plain ValueError
        # for a bad/unknown foreign id (e.g. "no project with id ..."). Not a
        # server fault -- the client sent a reference that doesn't resolve.
        return _problem(
            request,
            status_code=400,
            type_slug="invalid-request",
            title="Invalid Request",
            detail=str(exc),
        )

    # ------------------------------------------------------------------
    # D9 / D12 -- the sample-database safety gate
    # ------------------------------------------------------------------
    @app.exception_handler(CatalogAccessDeniedError)
    def _handle_catalog_denied(request: Request, exc: CatalogAccessDeniedError) -> JSONResponse:
        return _problem(
            request,
            status_code=403,
            type_slug="catalog-access-denied",
            title="Catalog Access Denied",
            detail=(
                "Querying the SQLite catalog (sqlite_master/sqlite_schema) is denied by "
                "default. Use the deterministic CRUD endpoints -- e.g. "
                "GET /schemas/{schema_id} or GET /model-versions/{version_id} -- to find "
                "out what tables and columns exist, or pass allow_catalog=true to opt in "
                "explicitly."
            ),
        )

    @app.exception_handler(UnsafeQueryError)
    def _handle_unsafe_query(request: Request, exc: UnsafeQueryError) -> JSONResponse:
        return _problem(
            request,
            status_code=400,
            type_slug="unsafe-query",
            title="Unsafe Query",
            detail=str(exc),
        )

    @app.exception_handler(DatabaseNotFoundError)
    def _handle_db_not_found(request: Request, exc: DatabaseNotFoundError) -> JSONResponse:
        return _problem(
            request,
            status_code=404,
            type_slug="database-not-found",
            title="Database Not Found",
            detail=str(exc),
        )

    @app.exception_handler(QueryTimeoutError)
    def _handle_query_timeout(request: Request, exc: QueryTimeoutError) -> JSONResponse:
        return _problem(
            request,
            status_code=504,
            type_slug="query-timeout",
            title="Query Timeout",
            detail=str(exc),
        )

    @app.exception_handler(InvalidIdentifierError)
    def _handle_invalid_identifier(request: Request, exc: InvalidIdentifierError) -> JSONResponse:
        return _problem(
            request,
            status_code=400,
            type_slug="invalid-identifier",
            title="Invalid Identifier",
            detail=str(exc),
        )

    @app.exception_handler(SampleDatabaseError)
    def _handle_sample_db_error(request: Request, exc: SampleDatabaseError) -> JSONResponse:
        return _problem(
            request,
            status_code=400,
            type_slug="sample-database-error",
            title="Sample Database Error",
            detail=str(exc),
        )

    # ------------------------------------------------------------------
    # t2s_core transport failures (design §5 / D9) -- 502/429/504, and the
    # provider's raw error body never appears below: every detail string is
    # fixed and generic; only the opaque request_id is echoed.
    # ------------------------------------------------------------------
    @app.exception_handler(RateLimited)
    def _handle_rate_limited(request: Request, exc: RateLimited) -> JSONResponse:
        return _problem(
            request,
            status_code=429,
            type_slug="rate-limited",
            title="Rate Limited",
            detail="The upstream inference provider rate-limited this request. Try again shortly.",
            request_id=exc.request_id,
        )

    @app.exception_handler(TruncatedResponse)
    def _handle_truncated(request: Request, exc: TruncatedResponse) -> JSONResponse:
        return _problem(
            request,
            status_code=502,
            type_slug="upstream-truncated",
            title="Upstream Response Truncated",
            detail=(
                "The upstream model's response was truncated and could not be completed "
                "within the retry budget."
            ),
            request_id=exc.request_id,
        )

    @app.exception_handler(UpstreamError)
    def _handle_upstream_error(request: Request, exc: UpstreamError) -> JSONResponse:
        return _problem(
            request,
            status_code=502,
            type_slug="upstream-error",
            title="Upstream Error",
            detail="The upstream inference provider returned an error.",
            request_id=exc.request_id,
        )

    @app.exception_handler(InvalidResponse)
    def _handle_invalid_response(request: Request, exc: InvalidResponse) -> JSONResponse:
        return _problem(
            request,
            status_code=502,
            type_slug="upstream-invalid-response",
            title="Upstream Invalid Response",
            detail=(
                "The upstream inference provider returned a response this service could "
                "not interpret."
            ),
            request_id=exc.request_id,
        )

    @app.exception_handler(TransportError)
    def _handle_transport_error(request: Request, exc: TransportError) -> JSONResponse:
        return _problem(
            request,
            status_code=504,
            type_slug="transport-error",
            title="Transport Error",
            detail="A network error occurred while contacting the upstream inference provider.",
        )

    @app.exception_handler(FixtureNotFound)
    def _handle_fixture_not_found(request: Request, exc: FixtureNotFound) -> JSONResponse:
        # Only reachable when the app is wired to a RecordedClient (D8) and
        # asked a question with no captured fixture -- an offline-wiring
        # problem, not a client error.
        return _problem(
            request,
            status_code=500,
            type_slug="fixture-not-found",
            title="No Recorded Fixture",
            detail="This offline deployment has no recorded fixture for this exact request.",
        )

    @app.exception_handler(T2SError)
    def _handle_t2s_error(request: Request, exc: T2SError) -> JSONResponse:
        return _problem(
            request,
            status_code=500,
            type_slug="internal-error",
            title="Internal Error",
            detail="An internal error occurred while generating the response.",
        )

    @app.exception_handler(Exception)
    def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        return _problem(
            request,
            status_code=500,
            type_slug="internal-error",
            title="Internal Error",
            detail="An unexpected internal error occurred.",
        )
