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


class _Limiter:
    def check(self, operation, request) -> None:
        return None


class _Runtime:
    trusted_origins = frozenset({"https://crm.synthetic.invalid"})
    login_attempt_limiter = _Limiter()

    def __init__(self) -> None:
        self.login_calls = 0
        self.revoke_calls = 0
        self.session = None

    def login(self, login, password):
        self.login_calls += 1
        return None

    def active_session(self, token):
        return self.session

    def revoke(self, token, csrf_token):
        self.revoke_calls += 1
        return False


def test_admin_session_error_contract_rejects_before_creating_or_revoking(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    runtime = _Runtime()
    client = TestClient(create_app(admin_session_runtime=runtime), base_url="https://crm.synthetic.invalid")
    valid_headers = {"Origin": "https://crm.synthetic.invalid", "Content-Type": "application/json", "X-Requested-With": "crm"}

    assert client.post("/api/v1/admin/session", json={"login": "owner", "password": "wrong"}).json() == {"error": {"code": "FORBIDDEN"}}
    assert client.post("/api/v1/admin/session", headers={**valid_headers, "Content-Type": "text/plain"}, json={"login": "owner", "password": "wrong"}).status_code == 415
    assert client.post("/api/v1/admin/session", headers={"Origin": "https://crm.synthetic.invalid", "Content-Type": "application/json"}, json={"login": "owner", "password": "wrong"}).json() == {"error": {"code": "FORBIDDEN"}}
    credentials = client.post("/api/v1/admin/session", headers=valid_headers, json={"login": "owner", "password": "wrong"})
    assert credentials.status_code == 401
    assert credentials.json() == {"error": {"code": "AUTH_REQUIRED"}}
    assert runtime.login_calls == 1

    assert client.get("/api/v1/admin/session").json() == {"error": {"code": "AUTH_REQUIRED"}}
    client.cookies.set("admin_session", "synthetic")
    runtime.session = {"csrf_secret": b"expected"}
    csrf = client.delete("/api/v1/admin/session", headers={"Origin": "https://crm.synthetic.invalid"})
    assert csrf.status_code == 403
    assert csrf.json() == {"error": {"code": "CSRF_FAILED"}}
    origin = client.delete("/api/v1/admin/session", headers={"Origin": "https://untrusted.invalid", "X-CSRF-Token": "expected"})
    assert origin.status_code == 403
    assert origin.json() == {"error": {"code": "FORBIDDEN"}}
    assert runtime.revoke_calls == 0
