"""Disposable PostgreSQL coverage for the API-001 owner visit command route."""

from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL, make_url

from sivka_burka_api.admin_commands import AdminCommandRuntime
from sivka_burka_api.admin_sessions import AdminSessionRuntime, hash_password
from sivka_burka_api.admin_visits import AdminVisitReadRuntime
from sivka_burka_api.main import create_app


ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://crm.synthetic.invalid"


def synthetic_template_url() -> URL:
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return make_url(line.removeprefix("DATABASE_URL="))
    raise RuntimeError("synthetic PostgreSQL template is unavailable")


@pytest.fixture()
def database(monkeypatch: pytest.MonkeyPatch) -> Engine:
    source = synthetic_template_url()
    target = source.set(database=f"sivka_api001_{uuid4().hex}")
    if not source.database or target.database == source.database:
        raise AssertionError("refusing to use the template database itself")
    admin_url = target.set(database="postgres")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    engine: Engine | None = None
    try:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{target.database}" WITH (FORCE)'))
            connection.execute(text(f'CREATE DATABASE "{target.database}"'))
        monkeypatch.setenv("DATABASE_URL", target.render_as_string(hide_password=False))
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
    def check(self, operation: str, request: object) -> None:
        assert operation == "admin-session-login"


class Lifetime:
    def session_lifetime(self) -> timedelta:
        return timedelta(hours=1)


def seed(engine: Engine) -> tuple[UUID, UUID]:
    owner_id, service_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO owner_accounts (id, login, password_hash, credentials_version) VALUES (:id, 'owner', :password, 1)"),
            {"id": owner_id, "password": hash_password("correct horse")},
        )
        connection.execute(
            text("INSERT INTO services (id, code, title, description, information, active, sort_order) VALUES (:id, 'ride', 'Synthetic ride', 'd', 'i', true, 1)"),
            {"id": service_id},
        )
    return owner_id, service_id


def client_for(engine: Engine) -> TestClient:
    return TestClient(
        create_app(
            admin_session_runtime=AdminSessionRuntime(engine, b"synthetic-session-secret", Limiter(), Lifetime(), frozenset({ORIGIN})),
            admin_command_runtime=AdminCommandRuntime(engine, b"synthetic-command-secret", 3),
            admin_visit_read_runtime=AdminVisitReadRuntime(engine),
        ),
        base_url=ORIGIN,
    )


def login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/admin/session",
        headers={"Origin": ORIGIN, "Content-Type": "application/json", "X-Requested-With": "crm"},
        json={"login": "owner", "password": "correct horse"},
    )
    assert response.status_code == 200
    return response.json()["data"]["csrf_token"]


def visit_payload(service_id: UUID, duration: int | None = 60) -> dict[str, object]:
    return {"service_id": str(service_id), "start_at": "2026-10-01T08:00:00Z", "duration_minutes": duration}


def command_headers(csrf_token: str, *, key: str = "planned-visit") -> dict[str, str]:
    return {"Origin": ORIGIN, "Content-Type": "application/json", "X-CSRF-Token": csrf_token, "Idempotency-Key": key}


def test_runtime_requires_only_explicit_safe_dependencies(database: Engine):
    with pytest.raises(ValueError):
        AdminCommandRuntime(database, b"", 1)
    with pytest.raises(ValueError):
        AdminCommandRuntime(database, b"secret", 0)
    assert AdminCommandRuntime(database, b"secret", 1).engine is database


def test_create_planned_visit_requires_session_origin_csrf_and_json(database: Engine):
    _, service_id = seed(database)
    client = client_for(database)
    payload = visit_payload(service_id)
    assert client.post("/api/v1/admin/visits", headers={"Origin": ORIGIN, "Content-Type": "application/json", "Idempotency-Key": "key"}, json=payload).json() == {"error": {"code": "AUTH_REQUIRED"}}
    csrf_token = login(client)
    for headers, status, code in (
        ({**command_headers(csrf_token), "Origin": "https://untrusted.invalid"}, 403, "FORBIDDEN"),
        ({**command_headers("wrong")}, 403, "CSRF_FAILED"),
        ({"Origin": ORIGIN, "X-CSRF-Token": csrf_token, "Idempotency-Key": "key", "Content-Type": "text/plain"}, 415, "UNSUPPORTED_MEDIA_TYPE"),
    ):
        response = client.post("/api/v1/admin/visits", headers=headers, content="{}")
        assert response.status_code == status and response.json() == {"error": {"code": code}}
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM visits")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM operation_receipts")).scalar_one() == 0


def test_create_replay_validation_and_calendar_read_boundaries(database: Engine):
    _, service_id = seed(database)
    client = client_for(database)
    csrf_token = login(client)
    headers = command_headers(csrf_token)
    for malformed in ({}, {**visit_payload(service_id), "csrf_token": "transport"}):
        response = client.post("/api/v1/admin/visits", headers=headers, json=malformed)
        assert response.status_code == 422 and response.json() == {"error": {"code": "VALIDATION_ERROR"}}
    for unavailable in (uuid4(),):
        response = client.post("/api/v1/admin/visits", headers={**headers, "Idempotency-Key": f"missing-{unavailable}"}, json=visit_payload(unavailable))
        assert response.status_code == 422 and response.json() == {"error": {"code": "OPTION_UNAVAILABLE"}}
    with database.begin() as connection:
        connection.execute(text("UPDATE services SET active=false WHERE id=:id"), {"id": service_id})
    inactive = client.post("/api/v1/admin/visits", headers={**headers, "Idempotency-Key": "inactive"}, json=visit_payload(service_id))
    assert inactive.status_code == 422 and inactive.json() == {"error": {"code": "OPTION_UNAVAILABLE"}}
    with database.begin() as connection:
        connection.execute(text("UPDATE services SET active=true WHERE id=:id"), {"id": service_id})
    created = client.post("/api/v1/admin/visits", headers=headers, json=visit_payload(service_id))
    assert created.status_code == 201 and created.headers["cache-control"] == "no-store"
    data = created.json()["data"]
    assert set(data) == {"command_id", "visit"}
    assert set(data["visit"]) == {"id", "version", "status"}
    assert data["visit"]["version"] == 1 and data["visit"]["status"] == "PLANNED"
    replay = client.post("/api/v1/admin/visits", headers=headers, json=visit_payload(service_id))
    assert replay.status_code == 200 and replay.headers["idempotent-replay"] == "true" and replay.json() == created.json()
    mismatch = client.post("/api/v1/admin/visits", headers=headers, json=visit_payload(service_id, 90))
    assert mismatch.status_code == 409 and mismatch.json() == {"error": {"code": "IDEMPOTENCY_MISMATCH"}}
    calendar = client.get("/api/v1/admin/visits", params={"from": "2026-10-01T00:00:00Z", "to": "2026-10-02T00:00:00Z"})
    assert calendar.status_code == 200
    assert [item["id"] for item in calendar.json()["data"]["items"]] == [data["visit"]["id"]]
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM visits")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM operation_receipts")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM change_events")).scalar_one() == 1
    with database.begin() as connection:
        connection.execute(text("UPDATE operation_receipts SET result_json=NULL, tombstoned_at=now() WHERE id=:id"), {"id": UUID(data["command_id"])})
    expired = client.post("/api/v1/admin/visits", headers=headers, json=visit_payload(service_id))
    assert expired.status_code == 410 and expired.json() == {"error": {"code": "RESULT_EXPIRED"}}
