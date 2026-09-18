"""Layer 1 CRUD: the deterministic surface (design §5). No natural language anywhere here."""

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


def test_project_create_get_list(client: TestClient) -> None:
    created = client.post("/projects", json={"name": "Acme", "slug": "acme"})
    assert created.status_code == 201
    body = created.json()
    assert body["slug"] == "acme"
    assert body["is_default"] is False

    fetched = client.get(f"/projects/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == body["id"]

    listed = client.get("/projects")
    assert listed.status_code == 200
    assert any(p["id"] == body["id"] for p in listed.json())


def test_project_not_found_is_a_problem_document(client: TestClient) -> None:
    response = client.get("/projects/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == 404
    assert body["type"].endswith("/not-found")
    assert "title" in body and "detail" in body


def test_session_defaults_to_the_default_project_when_omitted(client: TestClient) -> None:
    created = client.post("/sessions", json={})
    assert created.status_code == 201
    body = created.json()
    assert body["project_id"] is not None

    default_project = client.get("/projects").json()
    assert any(p["is_default"] for p in default_project)


def test_session_can_attach_multiple_data_models(client: TestClient) -> None:
    session = client.post("/sessions", json={}).json()
    model_a = client.post("/data-models", json={"name": "a"}).json()
    model_b = client.post("/data-models", json={"name": "b"}).json()

    r1 = client.post(
        f"/sessions/{session['id']}/data-models", json={"data_model_id": model_a["id"]}
    )
    r2 = client.post(
        f"/sessions/{session['id']}/data-models", json={"data_model_id": model_b["id"]}
    )
    assert r1.status_code == 200
    assert r2.status_code == 200


def test_full_model_version_schema_dataset_lifecycle(client: TestClient) -> None:
    model = client.post("/data-models", json={"name": "widgets model"}).json()
    assert model["name"] == "widgets model"

    version = client.post(f"/data-models/{model['id']}/versions", json={"graph": _GRAPH}).json()
    assert version["version"] == 1
    assert version["graph"]["tables"][0]["name"] == "widgets"

    latest = client.get(f"/data-models/{model['id']}/versions/latest").json()
    assert latest["id"] == version["id"]

    fetched_version = client.get(f"/model-versions/{version['id']}").json()
    assert fetched_version["id"] == version["id"]

    schema = client.post(f"/model-versions/{version['id']}/schemas", json={"dialect": "sqlite"})
    assert schema.status_code == 201
    schema_body = schema.json()
    assert "CREATE TABLE" in schema_body["ddl"].upper()
    assert schema_body["dialect"] == "sqlite"

    fetched_schema = client.get(f"/schemas/{schema_body['id']}")
    assert fetched_schema.status_code == 200
    assert fetched_schema.json()["ddl"] == schema_body["ddl"]

    listed_schemas = client.get(f"/model-versions/{version['id']}/schemas")
    assert listed_schemas.status_code == 200
    assert len(listed_schemas.json()) == 1

    dataset = client.post(
        f"/model-versions/{version['id']}/datasets",
        json={"name": "seed", "rows": {"widgets": [{"id": 1, "name": "sprocket"}]}},
    )
    assert dataset.status_code == 201
    dataset_body = dataset.json()
    assert dataset_body["rows"]["widgets"] == [{"id": 1, "name": "sprocket"}]

    fetched_dataset = client.get(f"/datasets/{dataset_body['id']}")
    assert fetched_dataset.status_code == 200


def test_unsupported_dialect_is_a_problem_not_a_500(client: TestClient) -> None:
    model = client.post("/data-models", json={"name": "m"}).json()
    version = client.post(f"/data-models/{model['id']}/versions", json={"graph": _GRAPH}).json()
    response = client.post(f"/model-versions/{version['id']}/schemas", json={"dialect": "mysql"})
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/problem+json")
