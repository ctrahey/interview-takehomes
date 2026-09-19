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


# ---------------------------------------------------------------------------
# Layer 3 -- the conversational surface (W15). Chris generates a client against
# this spec, so "it works" is not enough: the interesting payloads have to be
# typed in the document, not just in the Python.
# ---------------------------------------------------------------------------
def test_layer3_paths_are_present(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    for expected in [
        "/sessions/{session_id}/chat",
        "/sessions/{session_id}/activities",
        "/sessions/{session_id}/activities/stream",
    ]:
        assert expected in paths, expected


def test_the_chat_response_is_fully_typed(client: TestClient) -> None:
    schemas = client.get("/openapi.json").json()["components"]["schemas"]

    chat = schemas["ChatResponse"]["properties"]
    assert {"session_id", "utterance", "status", "plan", "results", "not_run", "activities"} <= set(
        chat
    )
    assert set(schemas["ChatResponse"]["properties"]["status"]["enum"]) == {
        "completed",
        "halted",
        "refused",
    }

    # Nothing interesting is a bare object: every nested payload is a $ref.
    assert chat["results"]["items"]["$ref"].endswith("/DirectiveResult")
    result = schemas["DirectiveResult"]["properties"]
    assert result["turn"]["$ref"].endswith("/TurnOut")

    turn = schemas["TurnOut"]["properties"]
    assert {"kind", "intent", "text", "sql", "ddl", "table", "attempts", "notes"} <= set(turn)
    assert set(turn["kind"]["enum"]) == {"answer", "clarification_needed", "error"}
    assert turn["attempts"]["items"]["$ref"].endswith("/Attempt")
    assert "TurnOut" in schemas and "DataTableOut" in schemas
    assert "Directive" in schemas and "Parameters" in schemas


def test_the_activity_log_and_its_stream_are_documented(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]

    activity = schemas["ActivityOut"]["properties"]
    assert {"seq", "session_id", "kind", "phase", "status", "at", "duration_ms"} <= set(activity)
    assert set(activity["phase"]["enum"]) == {"begin", "end"}

    page = schemas["ActivityPage"]["properties"]
    assert page["activities"]["items"]["$ref"].endswith("/ActivityOut")
    assert {"after_seq", "limit", "next_after_seq", "has_more"} <= set(page)

    stream = spec["paths"]["/sessions/{session_id}/activities/stream"]["get"]
    content = stream["responses"]["200"]["content"]
    assert "text/event-stream" in content
    assert content["text/event-stream"]["schema"]["$ref"].endswith("/ActivityOut")
    parameters = {p["name"] for p in stream["parameters"]}
    assert {"after_seq", "Last-Event-ID"} <= parameters
