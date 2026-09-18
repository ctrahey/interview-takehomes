"""Layer 2: `/text-to-sql/query` and `/text-to-sql/schema` (design §5).

Runs entirely offline against `t2s_core`'s shipped recorded fixtures (D8) --
real captured model output, replayed by `RecordedClient`, no network. Uses
the exact `QueryRequest`/`SchemaRequest` objects from
`t2s_core.fixtures.scenarios` so each request hashes to the fixture that was
captured for it.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi.testclient import TestClient

from t2s_core.fixtures.scenarios import QUERY_SCENARIOS, SCHEMA_SCENARIOS, Scenario, SchemaScenario


def _find[T: (Scenario, SchemaScenario)](scenarios: Sequence[T], name: str) -> T:
    return next(s for s in scenarios if s.name == name)


def test_valid_query_is_satisfiable_from_the_payload_alone(client: TestClient) -> None:
    scenario = _find(QUERY_SCENARIOS, "simple_count")
    response = client.post("/text-to-sql/query", json=scenario.request.model_dump())
    assert response.status_code == 200
    body = response.json()
    assert body["response_class"] == "valid"
    assert body["query"]
    assert body["metadata"]["dialect"] == "sqlite"


def test_clarification_needed_is_a_200_not_a_4xx(client: TestClient) -> None:
    scenario = _find(QUERY_SCENARIOS, "ambiguous_best_customers")
    response = client.post("/text-to-sql/query", json=scenario.request.model_dump())
    assert response.status_code == 200
    body = response.json()
    assert body["response_class"] == "clarification_needed"
    assert body["query"] is None
    assert body["prose"]


def test_semantic_error_is_a_200_not_a_4xx(client: TestClient) -> None:
    scenario = _find(QUERY_SCENARIOS, "unanswerable_salaries")
    response = client.post("/text-to-sql/query", json=scenario.request.model_dump())
    assert response.status_code == 200
    body = response.json()
    assert body["response_class"] == "error"
    assert body["error"]["code"] != "invalid_schema_ddl"


def test_unparseable_schema_ddl_is_a_422_not_a_model_call(client: TestClient) -> None:
    # No fixture exists for this request (and none is needed): t2s_core's own
    # preflight rejects the DDL before ever calling `client.complete`.
    response = client.post(
        "/text-to-sql/query",
        json={"question": "How many rows?", "schema_ddl": "NOT VALID SQL ((( at all"},
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"].endswith("/invalid-schema-ddl")


def test_query_request_extra_fields_are_rejected_as_validation_errors(client: TestClient) -> None:
    response = client.post(
        "/text-to-sql/query",
        json={"question": "x", "schema_ddl": "CREATE TABLE t (a INT)", "unexpected_field": 1},
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"].endswith("/validation-error")


def test_schema_generation_returns_ddl_and_entity_graph(client: TestClient) -> None:
    scenario = _find(SCHEMA_SCENARIOS, "library_lending")
    response = client.post("/text-to-sql/schema", json=scenario.request.model_dump())
    assert response.status_code == 200
    body = response.json()
    assert body["response_class"] == "valid"
    assert "CREATE TABLE" in body["query"].upper()
    assert body["entity_graph"] is not None
    assert len(body["entity_graph"]["tables"]) > 0


def test_schema_clarification_needed_has_no_entity_graph(client: TestClient) -> None:
    scenario = _find(SCHEMA_SCENARIOS, "too_thin")
    response = client.post("/text-to-sql/schema", json=scenario.request.model_dump())
    assert response.status_code == 200
    body = response.json()
    assert body["response_class"] == "clarification_needed"
    assert body["entity_graph"] is None


def test_injection_attempt_never_emits_ddl(client: TestClient) -> None:
    scenario = _find(QUERY_SCENARIOS, "injection_direct")
    response = client.post("/text-to-sql/query", json=scenario.request.model_dump())
    assert response.status_code == 200
    body = response.json()
    if body["query"]:
        assert "DROP" not in body["query"].upper()
