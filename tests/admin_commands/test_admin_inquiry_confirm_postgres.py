"""Disposable PostgreSQL route-contract coverage for owner inquiry confirmation."""

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
from sivka_burka_api.main import create_app
from sivka_burka_api.owner_commands import OwnerCommandService


ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://crm.synthetic.invalid"
COMMAND_SECRET = b"synthetic-command-secret"


def synthetic_template_url() -> URL:
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return make_url(line.removeprefix("DATABASE_URL="))
    raise RuntimeError("synthetic PostgreSQL template is unavailable")


@pytest.fixture()
def database(monkeypatch: pytest.MonkeyPatch) -> Engine:
    source = synthetic_template_url()
    target = source.set(database=f"sivka_api002_{uuid4().hex}")
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
            admin_command_runtime=AdminCommandRuntime(engine, COMMAND_SECRET, 3),
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


def headers(csrf_token: str, key: str = "confirm") -> dict[str, str]:
    return {"Origin": ORIGIN, "Content-Type": "application/json", "X-CSRF-Token": csrf_token, "Idempotency-Key": key}


def create_inquiry(engine: Engine, service_id: UUID, *, status: str = "NEW", duration: int | None = 60) -> UUID:
    option_id, contact_id, inquiry_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO service_options (id, service_id, code, duration_minutes, pricing_mode, price_minor, currency, active) VALUES (:id, :service_id, :code, :duration, 'FIXED_PER_PERSON', 100, 'RUB', true)"), {"id": option_id, "service_id": service_id, "code": f"option-{option_id.hex}", "duration": duration})
        connection.execute(text("INSERT INTO contact_cards (id) VALUES (:id)"), {"id": contact_id})
        connection.execute(text("""INSERT INTO inquiries (id, contact_id, source_kind, status, contact_snapshot, selection_snapshot, requester_name, contact_kind, contact_value)
            VALUES (:id, :contact_id, 'PHONE', :status, '{}'::jsonb, '{}'::jsonb, 'Synthetic', 'PHONE', '+70000000000')"""), {"id": inquiry_id, "contact_id": contact_id, "status": status})
        connection.execute(text("INSERT INTO inquiry_terms (inquiry_id, service_option_id, participants_count, duration_minutes, currency) VALUES (:inquiry_id, :option_id, 1, :duration, 'RUB')"), {"inquiry_id": inquiry_id, "option_id": option_id, "duration": duration})
    return inquiry_id


def visit(service: OwnerCommandService, service_id: UUID, key: str) -> UUID:
    return service.create_planned_visit({"service_id": str(service_id), "start_at": "2026-10-01T08:00:00Z", "duration_minutes": 60}, key).visit_id


def envelope(visit_id: UUID, *, inquiry_version: int = 1, visit_version: int = 1) -> dict[str, object]:
    return {"type": "CONFIRM", "expected_version": inquiry_version, "expected_visit_versions": {str(visit_id): visit_version}, "payload": {"visit_id": str(visit_id)}}


def test_confirm_requires_owner_browser_boundary_and_strict_envelope(database: Engine):
    owner_id, service_id = seed(database)
    inquiry_id = create_inquiry(database, service_id)
    target = f"/api/v1/admin/inquiries/{inquiry_id}/commands"
    client = client_for(database)
    payload = envelope(uuid4())
    unauthenticated = client.post(target, headers={"Origin": ORIGIN, "Content-Type": "application/json", "Idempotency-Key": "key"}, json=payload)
    assert unauthenticated.status_code == 401 and unauthenticated.json() == {"error": {"code": "AUTH_REQUIRED"}}
    csrf_token = login(client)
    for request_headers, content, status, code in (
        ({**headers(csrf_token), "Origin": "https://untrusted.invalid"}, "{}", 403, "FORBIDDEN"),
        ({**headers("wrong")}, "{}", 403, "CSRF_FAILED"),
        ({"Origin": ORIGIN, "X-CSRF-Token": csrf_token, "Idempotency-Key": "key", "Content-Type": "text/plain"}, "{}", 415, "UNSUPPORTED_MEDIA_TYPE"),
    ):
        response = client.post(target, headers=request_headers, content=content)
        assert response.status_code == status and response.json() == {"error": {"code": code}}
    command_headers = headers(csrf_token)
    missing_key_headers = dict(command_headers)
    del missing_key_headers["Idempotency-Key"]
    missing_key = client.post(target, headers=missing_key_headers, json=payload)
    assert missing_key.status_code == 422 and missing_key.json() == {"error": {"code": "VALIDATION_ERROR"}}
    malformed_json = client.post(target, headers=command_headers, content="not-json")
    assert malformed_json.status_code == 422 and malformed_json.json() == {"error": {"code": "VALIDATION_ERROR"}}
    for malformed in ({}, {**payload, "type": "COMPLETE"}, {"type": "CONFIRM", "expected_version": 1, "expected_visit_versions": {}, "payload": {"visit_id": str(uuid4())}, "extra": True}):
        rejected = client.post(target, headers=command_headers, json=malformed)
        assert rejected.status_code == 422 and rejected.json() == {"error": {"code": "VALIDATION_ERROR"}}
    bad_path = client.post("/api/v1/admin/inquiries/not-a-uuid/commands", headers=command_headers, json=payload)
    assert bad_path.status_code == 404 and bad_path.json() == {"error": {"code": "NOT_FOUND"}}
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM operation_receipts WHERE canonical_path LIKE '/api/v1/admin/inquiries/%'")) .scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM visit_participations")).scalar_one() == 0
    assert owner_id


@pytest.mark.parametrize("status", ["NEW", "NEGOTIATING"])
def test_confirm_returns_complete_stable_receipt_and_audits_once(database: Engine, status: str):
    owner_id, service_id = seed(database)
    inquiry_id = create_inquiry(database, service_id, status=status)
    service = OwnerCommandService(database, owner_id, hmac_secret=COMMAND_SECRET, digest_key_version=3)
    visit_id = visit(service, service_id, f"create-{status}")
    client = client_for(database)
    csrf_token = login(client)
    target = f"/api/v1/admin/inquiries/{inquiry_id}/commands"
    request_headers = headers(csrf_token, f"confirm-{status}")
    first = client.post(target, headers=request_headers, json=envelope(visit_id))
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    data = first.json()["data"]
    assert set(data) == {"command_id", "inquiry", "changed_visits"}
    assert data["inquiry"]["status"] == "CONFIRMED"
    assert data["inquiry"]["participation"]["visit_id"] == str(visit_id)
    assert data["changed_visits"] == [{"id": str(visit_id), "version": 2}]
    with database.begin() as connection:
        connection.execute(text("UPDATE inquiries SET requester_name='Later projection' WHERE id=:id"), {"id": inquiry_id})
    replay = client.post(target, headers=request_headers, json=envelope(visit_id))
    assert replay.status_code == 200 and replay.headers["idempotent-replay"] == "true"
    assert replay.json() == first.json()
    second_visit = visit(service, service_id, f"second-{status}")
    mismatch = client.post(target, headers=request_headers, json=envelope(second_visit, inquiry_version=2))
    assert mismatch.status_code == 409 and mismatch.json() == {"error": {"code": "IDEMPOTENCY_MISMATCH"}}
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM visit_participations WHERE inquiry_id=:id"), {"id": inquiry_id}).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM operation_receipts WHERE canonical_path=:path"), {"path": target}).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM change_events WHERE command_id=:id"), {"id": UUID(data["command_id"])}).scalar_one() == 1
    with database.begin() as connection:
        connection.execute(text("UPDATE operation_receipts SET result_json=NULL, tombstoned_at=now() WHERE id=:id"), {"id": UUID(data["command_id"])})
    expired = client.post(target, headers=request_headers, json=envelope(visit_id))
    assert expired.status_code == 410 and expired.json() == {"error": {"code": "RESULT_EXPIRED"}}


def test_confirm_maps_versions_plan_and_missing_targets_without_mutation(database: Engine):
    owner_id, service_id = seed(database)
    inquiry_id = create_inquiry(database, service_id)
    service = OwnerCommandService(database, owner_id, hmac_secret=COMMAND_SECRET, digest_key_version=3)
    visit_id = visit(service, service_id, "create")
    client = client_for(database)
    csrf_token = login(client)
    target = f"/api/v1/admin/inquiries/{inquiry_id}/commands"
    for key, payload, status, code in (
        ("empty-versions", {**envelope(visit_id), "expected_visit_versions": {}}, 422, "EXPECTED_VERSION_REQUIRED"),
        ("wrong-version", envelope(visit_id, inquiry_version=2), 409, "VERSION_CONFLICT"),
        ("unknown-visit", envelope(uuid4()), 404, "NOT_FOUND"),
    ):
        response = client.post(target, headers=headers(csrf_token, key), json=payload)
        assert response.status_code == status and response.json() == {"error": {"code": code}}
    unknown_inquiry = client.post(f"/api/v1/admin/inquiries/{uuid4()}/commands", headers=headers(csrf_token, "unknown-inquiry"), json=envelope(visit_id))
    assert unknown_inquiry.status_code == 404 and unknown_inquiry.json() == {"error": {"code": "NOT_FOUND"}}
    other_service = uuid4()
    with database.begin() as connection:
        connection.execute(text("INSERT INTO services (id, code, title, description, information, active, sort_order) VALUES (:id, :code, 'Other', 'd', 'i', true, 2)"), {"id": other_service, "code": f"other-{other_service.hex}"})
    incompatible = create_inquiry(database, other_service)
    plan = client.post(f"/api/v1/admin/inquiries/{incompatible}/commands", headers=headers(csrf_token, "plan-conflict"), json=envelope(visit_id))
    assert plan.status_code == 409 and plan.json() == {"error": {"code": "PLAN_CONFLICT"}}
    cancelled = create_inquiry(database, service_id, status="CANCELLED")
    invalid_transition = client.post(f"/api/v1/admin/inquiries/{cancelled}/commands", headers=headers(csrf_token, "cancelled"), json=envelope(visit_id))
    assert invalid_transition.status_code == 422 and invalid_transition.json() == {"error": {"code": "INVALID_TRANSITION"}}
    already_attached = create_inquiry(database, service_id)
    with database.begin() as connection:
        connection.execute(text("INSERT INTO visit_participations (id, inquiry_id, visit_id, joined_at) VALUES (:id, :inquiry_id, :visit_id, now())"), {"id": uuid4(), "inquiry_id": already_attached, "visit_id": visit_id})
    participation = client.post(f"/api/v1/admin/inquiries/{already_attached}/commands", headers=headers(csrf_token, "attached"), json=envelope(visit_id))
    assert participation.status_code == 409 and participation.json() == {"error": {"code": "PARTICIPATION_CONFLICT"}}
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM visit_participations")).scalar_one() == 1
        assert connection.execute(text("SELECT status, version FROM inquiries WHERE id=:id"), {"id": inquiry_id}).one() == ("NEW", 1)
        assert connection.execute(text("SELECT version FROM visits WHERE id=:id"), {"id": visit_id}).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM operation_receipts WHERE canonical_path LIKE '/api/v1/admin/inquiries/%'")) .scalar_one() == 0
