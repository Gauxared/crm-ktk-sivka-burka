from sivka_burka_api.main import create_app
from fastapi.testclient import TestClient


def test_public_catalog_fails_closed_without_runtime(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    response = TestClient(create_app()).get("/api/v1/public/catalog")
    assert response.status_code == 503
    assert response.json() == {"error": {"code": "PUBLIC_CATALOG_UNAVAILABLE"}}
    assert response.headers["cache-control"] == "no-store"
