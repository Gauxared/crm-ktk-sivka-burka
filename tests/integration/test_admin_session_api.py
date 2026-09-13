"""Disposable PostgreSQL integration checks for the owner session boundary."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL, make_url

from sivka_burka_api.admin_sessions import AdminSessionRuntime, LoginRateLimited, hash_password
from sivka_burka_api.main import create_app

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://crm.synthetic.invalid"


def configured_source_url() -> URL:
    value = os.environ.get("DATABASE_URL")
    if not value:
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_URL="):
                value = line.removeprefix("DATABASE_URL=")
                break
    if not value:
        raise RuntimeError("DATABASE_URL is not available")
    return make_url(value)


@pytest.fixture()
def database(monkeypatch: pytest.MonkeyPatch) -> Engine:
    source_url = configured_source_url()
    source_database = source_url.database
    target = source_url.set(database=f"sivka_auth_{uuid4().hex}")
    if target.database == source_database:
        raise AssertionError("refusing to migrate configured source DATABASE_URL")
    admin_url = target.set(database="postgres")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    engine = None
    try:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{target.database}" WITH (FORCE)'))
            connection.execute(text(f'CREATE DATABASE "{target.database}"'))
        monkeypatch.setenv("DATABASE_URL", target.render_as_string(hide_password=False))
        if configured_source_url().database != target.database:
            raise AssertionError("migration target was not the disposable database")
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        engine = create_engine(target)
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        admin.dispose()
        cleanup = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            with cleanup.connect() as connection:
                connection.execute(text(f'DROP DATABASE IF EXISTS "{target.database}" WITH (FORCE)'))
        finally:
            cleanup.dispose()


class Limiter:
    def __init__(self, reject: bool = False) -> None:
        self.reject = reject

    def check(self, operation: str, request) -> None:
        assert operation == "admin-session-login"
        if self.reject:
            raise LoginRateLimited()


class Lifetime:
    def __init__(self, value: timedelta = timedelta(hours=1)) -> None:
        self.value = value

    def session_lifetime(self) -> timedelta:
        return self.value


def client_for(database: Engine, *, reject: bool = False) -> TestClient:
    runtime = AdminSessionRuntime(database, b"synthetic-hmac-secret", Limiter(reject), Lifetime(), frozenset({ORIGIN}))
    return TestClient(create_app(admin_session_runtime=runtime), base_url=ORIGIN)


def provision(database: Engine, *, login: str = "owner", password: str = "correct horse", disabled: bool = False, password_hash: bytes | None = None, version: int = 1):
    owner_id = uuid4()
    with database.begin() as connection:
        connection.execute(
            text("INSERT INTO owner_accounts (id, login, password_hash, disabled_at, credentials_version) VALUES (:id, :login, :hash, :disabled_at, :version)"),
            {"id": owner_id, "login": login, "hash": password_hash or hash_password(password), "disabled_at": datetime.now(timezone.utc) if disabled else None, "version": version},
        )
    return owner_id


def login_headers() -> dict[str, str]:
    return {"Origin": ORIGIN, "Content-Type": "application/json", "X-Requested-With": "crm"}


def login(client: TestClient, password: str = "correct horse"):
    return client.post("/api/v1/admin/session", headers=login_headers(), json={"login": "owner", "password": password})


def test_login_read_and_sign_out_are_server_side_and_protected(database):
    owner_id = provision(database)
    client = client_for(database)
    response = login(client)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["owner_id"] == str(owner_id)
    cookie = response.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    assert "domain=" not in cookie
    with database.connect() as connection:
        stored = connection.execute(text("SELECT token_digest, csrf_secret FROM owner_sessions")).mappings().one()
        assert stored["token_digest"] != client.cookies.get("admin_session").encode("ascii")
        assert stored["csrf_secret"].decode("ascii") == data["csrf_token"]
    assert client.get("/api/v1/admin/session").json()["data"] == data
    signed_out = client.delete("/api/v1/admin/session", headers={"Origin": ORIGIN, "X-CSRF-Token": data["csrf_token"]})
    assert signed_out.status_code == 200
    assert signed_out.json() == {"data": {"signed_out": True}}
    with database.connect() as connection:
        assert connection.execute(text("SELECT revoked_at IS NOT NULL FROM owner_sessions")).scalar_one()
    assert client.get("/api/v1/admin/session").status_code == 401


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://untrusted.invalid", "Content-Type": "application/json", "X-Requested-With": "crm"}, {"Origin": ORIGIN, "Content-Type": "text/plain", "X-Requested-With": "crm"}, {"Origin": ORIGIN, "Content-Type": "application/json"}])
def test_login_rejects_missing_or_untrusted_browser_contract(database, headers):
    provision(database)
    response = client_for(database).post("/api/v1/admin/session", headers=headers, json={"login": "owner", "password": "correct horse"})
    assert response.status_code == 400
    assert response.json() == {"error": {"code": "INVALID_REQUEST"}}


@pytest.mark.parametrize("kind", ["unknown", "disabled", "invalid", "malformed"])
def test_private_login_failures_are_indistinguishable_and_create_no_session(database, kind):
    if kind == "unknown":
        provision(database)
        payload = {"login": "not-owner", "password": "correct horse"}
    elif kind == "disabled":
        provision(database, disabled=True)
        payload = {"login": "owner", "password": "correct horse"}
    elif kind == "malformed":
        provision(database, password_hash=b"not-a-password-hash")
        payload = {"login": "owner", "password": "correct horse"}
    else:
        provision(database)
        payload = {"login": "owner", "password": "wrong"}
    response = client_for(database).post("/api/v1/admin/session", headers=login_headers(), json=payload)
    assert response.status_code == 401
    assert response.json() == {"error": {"code": "ADMIN_SESSION_UNAUTHORIZED"}}
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM owner_sessions")).scalar_one() == 0


def test_limiter_and_invalid_csrf_do_not_reveal_or_revoke(database):
    provision(database)
    limited = client_for(database, reject=True).post("/api/v1/admin/session", headers=login_headers(), json={"login": "owner", "password": "wrong"})
    assert limited.status_code == 429
    client = client_for(database)
    token = login(client).json()["data"]["csrf_token"]
    rejected = client.delete("/api/v1/admin/session", headers={"Origin": ORIGIN, "X-CSRF-Token": "wrong"})
    assert rejected.status_code == 401
    assert client.get("/api/v1/admin/session").status_code == 200
    assert client.delete("/api/v1/admin/session", headers={"Origin": "https://untrusted.invalid", "X-CSRF-Token": token}).status_code == 401


@pytest.mark.parametrize("change", ["expired", "revoked", "version", "disabled"])
def test_server_validation_rejects_invalidated_sessions(database, change):
    provision(database)
    client = client_for(database)
    assert login(client).status_code == 200
    with database.begin() as connection:
        if change == "expired":
            connection.execute(text("UPDATE owner_sessions SET expires_at = created_at + interval '1 microsecond'"))
        elif change == "revoked":
            connection.execute(text("UPDATE owner_sessions SET revoked_at = now()"))
        elif change == "version":
            connection.execute(text("UPDATE owner_accounts SET credentials_version = credentials_version + 1"))
        else:
            connection.execute(text("UPDATE owner_accounts SET disabled_at = now()"))
    assert client.get("/api/v1/admin/session").status_code == 401
