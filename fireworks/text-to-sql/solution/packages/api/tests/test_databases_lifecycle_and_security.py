"""Layer 1: the sample-database lifecycle, plus its D9/D12 security guarantees.

D9: a write attempted through `/databases/{id}/query` is refused.
D12: catalog access (`sqlite_master`/`sqlite_schema`) is refused by default and
permitted under the explicit `allow_catalog` opt-in.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

_GRAPH = {
    "tables": [
        {
            "name": "widgets",
            "columns": [
                {"name": "id", "type": "INT", "nullable": False},
                {"name": "name", "type": "TEXT", "nullable": False},
            ],
            "primary_key": ["id"],
        }
    ]
}


def _make_database(client: TestClient) -> tuple[str, str]:
    """Returns (database_id, schema's DDL) with the widgets table created and loaded."""
    model = client.post("/data-models", json={"name": "m"}).json()
    version = client.post(f"/data-models/{model['id']}/versions", json={"graph": _GRAPH}).json()
    schema = client.post(
        f"/model-versions/{version['id']}/schemas", json={"dialect": "sqlite"}
    ).json()
    database = client.post("/databases", json={"schema_id": schema["id"]})
    assert database.status_code == 201
    database_id = database.json()["id"]

    dataset = client.post(
        f"/model-versions/{version['id']}/datasets",
        json={"name": "seed", "rows": {"widgets": [{"id": 1, "name": "sprocket"}]}},
    ).json()
    loaded = client.post(f"/databases/{database_id}/load", json={"dataset_id": dataset["id"]})
    assert loaded.status_code == 200
    assert loaded.json()["rows_inserted"] == 1
    return database_id, schema["ddl"]


def test_full_lifecycle_create_load_query_destroy(client: TestClient) -> None:
    database_id, _ = _make_database(client)

    got = client.get(f"/databases/{database_id}")
    assert got.status_code == 200
    assert got.json()["status"] == "loaded"

    result = client.post(
        f"/databases/{database_id}/query", json={"sql": "SELECT id, name FROM widgets"}
    )
    assert result.status_code == 200
    body = result.json()
    assert body["columns"] == ["id", "name"]
    assert body["rows"] == [[1, "sprocket"]]
    assert body["truncated"] is False

    destroyed = client.delete(f"/databases/{database_id}")
    assert destroyed.status_code == 204

    after = client.get(f"/databases/{database_id}")
    assert after.json()["status"] == "destroyed"


def test_database_not_found_is_a_404_problem(client: TestClient) -> None:
    response = client.post(
        "/databases/00000000-0000-0000-0000-000000000000/query", json={"sql": "SELECT 1"}
    )
    assert response.status_code == 404
    assert response.json()["type"].endswith("/database-not-found")


# ---------------------------------------------------------------------------
# D9 -- a write attempted through the query endpoint is refused
# ---------------------------------------------------------------------------


def test_write_attempt_through_query_endpoint_is_refused(client: TestClient) -> None:
    database_id, _ = _make_database(client)

    for sql in [
        "DELETE FROM widgets",
        "INSERT INTO widgets VALUES (99, 'hacked')",
        "DROP TABLE widgets",
        "UPDATE widgets SET name = 'x'",
        "SELECT 1; DROP TABLE widgets;",
    ]:
        response = client.post(f"/databases/{database_id}/query", json={"sql": sql})
        assert response.status_code == 400, sql
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["type"].endswith("/unsafe-query")

    # The attempted writes above did not go through.
    check = client.post(
        f"/databases/{database_id}/query", json={"sql": "SELECT count(*) AS n FROM widgets"}
    )
    assert check.json()["rows"] == [[1]]


# ---------------------------------------------------------------------------
# D12 -- catalog access denied by default, permitted with explicit opt-in
# ---------------------------------------------------------------------------


def test_catalog_access_denied_by_default(client: TestClient) -> None:
    database_id, _ = _make_database(client)

    response = client.post(
        f"/databases/{database_id}/query", json={"sql": "SELECT sql FROM sqlite_master"}
    )
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"].endswith("/catalog-access-denied")
    # The refusal points at the deterministic alternative.
    assert "schemas" in body["detail"].lower() or "schema" in body["detail"].lower()


def test_catalog_access_permitted_with_explicit_opt_in(client: TestClient) -> None:
    database_id, _ = _make_database(client)

    response = client.post(
        f"/databases/{database_id}/query",
        json={"sql": "SELECT name FROM sqlite_master WHERE type = 'table'", "allow_catalog": True},
    )
    assert response.status_code == 200
    names = {row[0] for row in response.json()["rows"]}
    assert "widgets" in names


def test_catalog_access_denial_default_is_false_when_field_omitted(client: TestClient) -> None:
    database_id, _ = _make_database(client)
    # `allow_catalog` is entirely absent from the body -- must still default closed.
    response = client.post(
        f"/databases/{database_id}/query", json={"sql": "SELECT * FROM sqlite_schema"}
    )
    assert response.status_code == 403
