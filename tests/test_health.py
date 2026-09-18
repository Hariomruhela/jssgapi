from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_health_check():
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["environment"] in {"development", "staging", "production"}


def test_ping():
    with TestClient(app) as client:
        response = client.get("/api/v1/ping")
    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "message": "pong",
        "data": None,
    }


def test_openapi_schema_available():
    with TestClient(app) as client:
        response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "JSSG API"


def test_docs_available():
    with TestClient(app) as client:
        response = client.get("/docs")
    assert response.status_code == 200
