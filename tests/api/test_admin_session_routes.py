from datetime import timedelta

from fastapi.testclient import TestClient

from sivka_burka_api.main import create_app


def test_admin_sessions_fail_closed_without_runtime(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    client = TestClient(create_app())
    for method in (client.post, client.get, client.delete):
        response = method("/api/v1/admin/session")
        assert response.status_code == 503
        assert response.json() == {"error": {"code": "ADMIN_SESSION_UNAVAILABLE"}}
        assert response.headers["cache-control"] == "no-store"
