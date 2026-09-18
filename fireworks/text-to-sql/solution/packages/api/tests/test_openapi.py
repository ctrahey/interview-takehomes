"""The OpenAPI spec generates cleanly and layer 2's schemas are documented (design §5)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_openapi_spec_generates_and_is_valid_json(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    assert spec["openapi"].startswith("3.")
    assert "paths" in spec and "components" in spec


def test_layer1_and_layer2_paths_are_present(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    for expected in [
        "/projects",
        "/sessions",
        "/data-models",
        "/model-versions/{version_id}/schemas",
        "/schemas/{schema_id}",
        "/model-versions/{version_id}/datasets",
        "/databases",
        "/databases/{database_id}/load",
        "/databases/{database_id}/query",
        "/text-to-sql/query",
        "/text-to-sql/schema",
    ]:
        assert expected in paths, expected


def test_text_to_sql_query_schema_is_documented(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    assert "QueryRequest" in schemas
    request_props = schemas["QueryRequest"]["properties"]
    assert {"question", "schema_ddl", "dialect", "session_summary", "max_repair_attempts"} <= set(
        request_props
    )

    assert "QueryResult" in schemas
    result_props = schemas["QueryResult"]["properties"]
    assert {"response_class", "query", "prose", "error", "metadata"} <= set(result_props)


def test_text_to_sql_schema_response_documents_the_entity_graph(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    response_schema = schemas["SchemaQueryResponse"]["properties"]
    assert "entity_graph" in response_schema
    assert "entity_graph_warnings" in response_schema


def test_database_query_request_documents_the_d12_opt_in(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    props = schemas["DatabaseQueryRequest"]["properties"]
    assert "allow_catalog" in props
    assert props["allow_catalog"]["default"] is False
