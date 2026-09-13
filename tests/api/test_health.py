from fastapi.testclient import TestClient

from sivka_burka_api.main import create_app


def test_health_is_live_without_database(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "sivka-burka-api",
        "environment": "test",
    }
