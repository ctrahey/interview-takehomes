"""CORS is what lets a browser UI talk to this API -- and what stops any page from doing so."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from t2s_api.app import DEFAULT_CORS_ORIGINS, create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.mark.parametrize("origin", DEFAULT_CORS_ORIGINS)
def test_local_dev_origins_are_allowed_out_of_the_box(client: TestClient, origin: str) -> None:
    response = client.options(
        "/projects",
        headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


def test_an_unknown_origin_is_refused(client: TestClient) -> None:
    """Not a wildcard. This API creates and queries databases."""
    response = client.options(
        "/projects",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert response.headers.get("access-control-allow-origin") is None


def test_origins_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T2S_CORS_ORIGINS", "https://ui.example, https://other.example")
    response = TestClient(create_app()).options(
        "/projects",
        headers={"Origin": "https://ui.example", "Access-Control-Request-Method": "POST"},
    )
    assert response.headers["access-control-allow-origin"] == "https://ui.example"
