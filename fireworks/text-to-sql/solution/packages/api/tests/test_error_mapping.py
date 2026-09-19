"""t2s_core's transport failures, mapped to HTTP problem responses (design §5).

`TruncatedResponse`, `RateLimited`, `UpstreamError` (and friends) are raised
*deliberately* out of `t2s_core.generate_query`/`generate_schema` for the API
layer to map -- see `t2s_core.errors`. Exercised here with a double (the
`raising_client` fixture from `conftest.py`) that raises them directly, so
this suite never depends on actually exhausting a live retry budget.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from t2s_api.app import create_app
from t2s_core.errors import (
    FixtureNotFound,
    InvalidResponse,
    RateLimited,
    TransportError,
    TruncatedResponse,
    UpstreamError,
)
from t2s_core.ports import InferenceClient

_VALID_PAYLOAD = {
    "question": "How many widgets are there?",
    "schema_ddl": "CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT NOT NULL)",
}

# The secret sauce a real Fireworks error body might contain -- must never
# reach the client. Asserted against on every case below.
_FORBIDDEN_SNIPPETS = ["sk-should-never-leak", "super-secret-fireworks-body-detail"]

MakeClient = Callable[[InferenceClient], TestClient]


def _assert_no_leak(response_text: str) -> None:
    for snippet in _FORBIDDEN_SNIPPETS:
        assert snippet not in response_text


def test_rate_limited_maps_to_429(make_client: MakeClient, raising_client: type) -> None:
    exc = RateLimited(
        f"Fireworks returned HTTP 429: {_FORBIDDEN_SNIPPETS[0]}",
        request_id="req-abc",
        code="rate_limit_exceeded",
    )
    client = make_client(raising_client(exc))
    response = client.post("/text-to-sql/query", json=_VALID_PAYLOAD)
    assert response.status_code == 429
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"].endswith("/rate-limited")
    assert body.get("request_id") == "req-abc"
    _assert_no_leak(response.text)


def test_truncated_response_maps_to_502(make_client: MakeClient, raising_client: type) -> None:
    exc = TruncatedResponse(
        f"completion hit the token budget: {_FORBIDDEN_SNIPPETS[1]}",
        max_tokens=2000,
        request_id="req-trunc",
    )
    client = make_client(raising_client(exc))
    response = client.post("/text-to-sql/query", json=_VALID_PAYLOAD)
    assert response.status_code == 502
    assert response.json()["type"].endswith("/upstream-truncated")
    _assert_no_leak(response.text)


def test_upstream_error_maps_to_502(make_client: MakeClient, raising_client: type) -> None:
    exc = UpstreamError(
        f"Fireworks returned HTTP 500: {_FORBIDDEN_SNIPPETS[0]}",
        request_id="req-500",
        code="internal_error",
    )
    client = make_client(raising_client(exc))
    response = client.post("/text-to-sql/query", json=_VALID_PAYLOAD)
    assert response.status_code == 502
    assert response.json()["type"].endswith("/upstream-error")
    _assert_no_leak(response.text)


def test_invalid_response_maps_to_502(make_client: MakeClient, raising_client: type) -> None:
    exc = InvalidResponse(f"no choices: {_FORBIDDEN_SNIPPETS[1]}", request_id="req-empty")
    client = make_client(raising_client(exc))
    response = client.post("/text-to-sql/query", json=_VALID_PAYLOAD)
    assert response.status_code == 502
    assert response.json()["type"].endswith("/upstream-invalid-response")
    _assert_no_leak(response.text)


def test_transport_error_maps_to_504(make_client: MakeClient, raising_client: type) -> None:
    exc = TransportError(f"connect error: {_FORBIDDEN_SNIPPETS[0]}")
    client = make_client(raising_client(exc))
    response = client.post("/text-to-sql/query", json=_VALID_PAYLOAD)
    assert response.status_code == 504
    assert response.json()["type"].endswith("/transport-error")
    _assert_no_leak(response.text)


def test_fixture_not_found_maps_to_500_not_a_client_error(
    make_client: MakeClient, raising_client: type
) -> None:
    exc = FixtureNotFound("deadbeef", "/tmp/nonexistent-fixtures")
    client = make_client(raising_client(exc))
    response = client.post("/text-to-sql/query", json=_VALID_PAYLOAD)
    assert response.status_code == 500
    assert response.json()["type"].endswith("/fixture-not-found")


def test_same_mapping_applies_to_the_schema_endpoint(
    make_client: MakeClient, raising_client: type
) -> None:
    exc = RateLimited("rate limited", request_id="req-schema")
    client = make_client(raising_client(exc))
    response = client.post("/text-to-sql/schema", json={"description": "A small library catalog."})
    assert response.status_code == 429
    assert response.json()["type"].endswith("/rate-limited")


def test_api_key_never_appears_anywhere_in_a_response(
    make_client: MakeClient, raising_client: type
) -> None:
    # No real key exists in this offline test (config.api_key is never
    # constructed here), but the *shape* of a would-be key must not be
    # echoed even when the underlying exception message happens to carry
    # provider text -- this is exactly what the doubles above simulate. Do a
    # last blanket sweep across the response's raw text.
    exc = UpstreamError(
        "Authorization: Bearer fw-fake-would-be-secret-1234567890", request_id="req-key-check"
    )
    client = make_client(raising_client(exc))
    response = client.post("/text-to-sql/query", json=_VALID_PAYLOAD)
    assert "fw-fake-would-be-secret-1234567890" not in response.text
    assert "Authorization" not in response.text


def test_a_missing_credential_is_503_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """No key is a deployment condition, not a server fault.

    It surfaced as a generic 500 "unexpected internal error", which tells an
    operator nothing and reads like a crash. Layer-1 CRUD keeps working.
    """
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    app = create_app()  # no injected client: the lazy live path
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/text-to-sql/query",
        json={
            "question": "how many customers are there?",
            "schema_ddl": "CREATE TABLE customers (id INTEGER PRIMARY KEY);",
            "dialect": "sqlite",
        },
    )

    assert response.status_code == 503
    body = response.json()
    assert body["title"] == "Inference Unavailable"
    assert "FIREWORKS_API_KEY" in body["detail"]
    assert response.headers["content-type"].startswith("application/problem+json")

    # The deterministic layer is unaffected -- that is why the client is lazy.
    created = client.post("/projects", json={"name": "still works", "slug": "still-works"})
    assert created.status_code == 201
