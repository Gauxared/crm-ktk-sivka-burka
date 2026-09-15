"""Disposable PostgreSQL route-contract coverage for manual owner cash notes."""

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
    target = source.set(database=f"sivka_api003_{uuid4().hex}")
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
        connection.execute(text("INSERT INTO owner_accounts (id, login, password_hash, credentials_version) VALUES (:id, 'owner', :password, 1)"), {"id": owner_id, "password": hash_password("correct horse")})
        connection.execute(text("INSERT INTO services (id, code, title, description, information, active, sort_order) VALUES (:id, 'ride', 'Synthetic ride', 'd', 'i', true, 1)"), {"id": service_id})
    return owner_id, service_id


def create_inquiry(engine: Engine, service_id: UUID, *, status: str = "NEW") -> UUID:
    option_id, contact_id, inquiry_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO service_options (id, service_id, code, duration_minutes, pricing_mode, price_minor, currency, active) VALUES (:id, :service_id, :code, 60, 'FIXED_PER_PERSON', 100, 'RUB', true)"), {"id": option_id, "service_id": service_id, "code": f"option-{option_id.hex}"})
        connection.execute(text("INSERT INTO contact_cards (id) VALUES (:id)"), {"id": contact_id})
        connection.execute(text("""INSERT INTO inquiries (id, contact_id, source_kind, status, contact_snapshot, selection_snapshot, requester_name, contact_kind, contact_value)
            VALUES (:id, :contact_id, 'PHONE', :status, '{}'::jsonb, '{}'::jsonb, 'Synthetic', 'PHONE', '+70000000000')"""), {"id": inquiry_id, "contact_id": contact_id, "status": status})
        connection.execute(text("INSERT INTO inquiry_terms (inquiry_id, service_option_id, participants_count, duration_minutes, currency) VALUES (:inquiry_id, :option_id, 1, 60, 'RUB')"), {"inquiry_id": inquiry_id, "option_id": option_id})
    return inquiry_id


def client_for(engine: Engine) -> TestClient:
    return TestClient(create_app(
        admin_session_runtime=AdminSessionRuntime(engine, b"synthetic-session-secret", Limiter(), Lifetime(), frozenset({ORIGIN})),
        admin_command_runtime=AdminCommandRuntime(engine, COMMAND_SECRET, 3),
    ), base_url=ORIGIN)


def login(client: TestClient) -> str:
    response = client.post("/api/v1/admin/session", headers={"Origin": ORIGIN, "Content-Type": "application/json", "X-Requested-With": "crm"}, json={"login": "owner", "password": "correct horse"})
    assert response.status_code == 200
    return response.json()["data"]["csrf_token"]


def headers(csrf_token: str, key: str = "cash-note") -> dict[str, str]:
    return {"Origin": ORIGIN, "Content-Type": "application/json", "X-CSRF-Token": csrf_token, "Idempotency-Key": key}


def payload(*, inquiry_version: int = 1, kind: str = "RECEIPT", visit_versions: dict[str, int] | None = None, amount: int = 175_000) -> dict[str, object]:
    result: dict[str, object] = {"expected_version": inquiry_version, "kind": kind, "amount_minor": amount, "occurred_at": "2026-10-01T08:00:00Z", "note": "Synthetic cash fact"}
    if visit_versions is not None:
        result["expected_visit_versions"] = visit_versions
    return result


def create_and_confirm(service: OwnerCommandService, inquiry_id: UUID, service_id: UUID) -> UUID:
    visit_id = service.create_planned_visit({"service_id": str(service_id), "start_at": "2026-10-01T08:00:00Z", "duration_minutes": 60}, "planned").visit_id
    service.confirm_inquiry(inquiry_id, {"expected_version": 1, "expected_visit_versions": {str(visit_id): 1}, "payload": {"visit_id": str(visit_id)}}, "confirm")
    return visit_id


def correction_payload(note_id: UUID, *, expected_version: int = 2, replacement: dict[str, object] | None = None, reason: str = "Factual correction", visit_versions: dict[str, int] | None = None) -> dict[str, object]:
    result: dict[str, object] = {"expected_version": expected_version, "reason": reason, "replacement": replacement}
    if visit_versions is not None:
        result["expected_visit_versions"] = visit_versions
    return result


def test_cash_note_requires_owner_browser_boundary_and_strict_body(database: Engine):
    _, service_id = seed(database)
    inquiry_id = create_inquiry(database, service_id)
    target = f"/api/v1/admin/inquiries/{inquiry_id}/cash-notes"
    client = client_for(database)
    body = payload()
    unauthenticated = client.post(target, headers={"Origin": ORIGIN, "Content-Type": "application/json", "Idempotency-Key": "key"}, json=body)
    assert unauthenticated.status_code == 401 and unauthenticated.json() == {"error": {"code": "AUTH_REQUIRED"}}
    csrf_token = login(client)
    for request_headers, content, status, code in (
        ({**headers(csrf_token), "Origin": "https://untrusted.invalid"}, "{}", 403, "FORBIDDEN"),
        ({**headers("wrong")}, "{}", 403, "CSRF_FAILED"),
        ({"Origin": ORIGIN, "X-CSRF-Token": csrf_token, "Idempotency-Key": "key", "Content-Type": "text/plain"}, "{}", 415, "UNSUPPORTED_MEDIA_TYPE"),
    ):
        response = client.post(target, headers=request_headers, content=content)
        assert response.status_code == status and response.json() == {"error": {"code": code}}
    valid_headers = headers(csrf_token)
    for malformed, code in (({}, "EXPECTED_VERSION_REQUIRED"), ({"expected_version": 1}, "VALIDATION_ERROR"), ({**body, "extra": True}, "VALIDATION_ERROR")):
        response = client.post(target, headers=valid_headers, json=malformed)
        assert response.status_code == 422 and response.json() == {"error": {"code": code}}
    missing_key = client.post(target, headers={key: value for key, value in valid_headers.items() if key != "Idempotency-Key"}, json=body)
    assert missing_key.status_code == 422 and missing_key.json() == {"error": {"code": "VALIDATION_ERROR"}}
    malformed_json = client.post(target, headers=valid_headers, content="not-json")
    assert malformed_json.status_code == 422 and malformed_json.json() == {"error": {"code": "VALIDATION_ERROR"}}
    invalid_amount = client.post(target, headers={**valid_headers, "Idempotency-Key": "bad-amount"}, json=payload(amount=0))
    assert invalid_amount.status_code == 422 and invalid_amount.json() == {"error": {"code": "VALIDATION_ERROR"}}
    with database.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM cash_notes")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM operation_receipts WHERE canonical_path=:path"), {"path": target}).scalar_one() == 0


def test_receipt_is_idempotent_audited_and_does_not_change_status(database: Engine):
    _, service_id = seed(database)
    inquiry_id = create_inquiry(database, service_id)
    client = client_for(database)
    csrf_token = login(client)
    target = f"/api/v1/admin/inquiries/{inquiry_id}/cash-notes"
    request_headers = headers(csrf_token, "receipt")
    first = client.post(target, headers=request_headers, json=payload())
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    data = first.json()["data"]
    assert set(data) == {"command_id", "inquiry", "changed_visits"}
    assert data["inquiry"]["status"] == "NEW" and data["inquiry"]["version"] == 2
    assert data["inquiry"]["cash_notes"] == [{**data["inquiry"]["cash_notes"][0], "kind": "RECEIPT", "amount_minor": 175_000, "currency": "RUB", "note": "Synthetic cash fact"}]
    assert data["changed_visits"] == []
    with database.begin() as connection:
        connection.execute(text("UPDATE inquiries SET requester_name='Later projection' WHERE id=:id"), {"id": inquiry_id})
    replay = client.post(target, headers=request_headers, json=payload())
    assert replay.status_code == 200 and replay.headers["idempotent-replay"] == "true" and replay.json() == first.json()
    mismatch = client.post(target, headers=request_headers, json=payload(amount=1))
    assert mismatch.status_code == 409 and mismatch.json() == {"error": {"code": "IDEMPOTENCY_MISMATCH"}}
    with database.connect() as connection:
        assert connection.execute(text("SELECT status, version FROM inquiries WHERE id=:id"), {"id": inquiry_id}).one() == ("NEW", 2)
        assert connection.execute(text("SELECT count(*) FROM cash_notes WHERE inquiry_id=:id"), {"id": inquiry_id}).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM operation_receipts WHERE canonical_path=:path"), {"path": target}).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM change_events WHERE command_id=:id AND action='CASH_NOTE_RECORDED'"), {"id": UUID(data["command_id"])}).scalar_one() == 1
    with database.begin() as connection:
        connection.execute(text("UPDATE operation_receipts SET result_json=NULL, tombstoned_at=now() WHERE id=:id"), {"id": UUID(data["command_id"])})
    expired = client.post(target, headers=request_headers, json=payload())
    assert expired.status_code == 410 and expired.json() == {"error": {"code": "RESULT_EXPIRED"}}


def test_refund_enforces_planned_versions_and_preserves_confirmed_status(database: Engine):
    owner_id, service_id = seed(database)
    unplanned_id = create_inquiry(database, service_id)
    service = OwnerCommandService(database, owner_id, hmac_secret=COMMAND_SECRET, digest_key_version=3)
    planned_id = create_inquiry(database, service_id)
    visit_id = create_and_confirm(service, planned_id, service_id)
    client = client_for(database)
    csrf_token = login(client)
    unplanned_target = f"/api/v1/admin/inquiries/{unplanned_id}/cash-notes"
    unexpected_visit = client.post(unplanned_target, headers=headers(csrf_token, "unplanned-visit"), json=payload(visit_versions={str(visit_id): 2}))
    assert unexpected_visit.status_code == 422 and unexpected_visit.json() == {"error": {"code": "EXPECTED_VERSION_REQUIRED"}}
    target = f"/api/v1/admin/inquiries/{planned_id}/cash-notes"
    stale = client.post(target, headers=headers(csrf_token, "stale"), json=payload(inquiry_version=1, kind="REFUND_NOTE", visit_versions={str(visit_id): 2}))
    assert stale.status_code == 409 and stale.json() == {"error": {"code": "VERSION_CONFLICT"}}
    wrong_visit = client.post(target, headers=headers(csrf_token, "wrong-visit"), json=payload(inquiry_version=2, kind="REFUND_NOTE", visit_versions={}))
    assert wrong_visit.status_code == 422 and wrong_visit.json() == {"error": {"code": "EXPECTED_VERSION_REQUIRED"}}
    recorded = client.post(target, headers=headers(csrf_token, "refund"), json=payload(inquiry_version=2, kind="REFUND_NOTE", visit_versions={str(visit_id): 2}))
    assert recorded.status_code == 200
    data = recorded.json()["data"]
    assert data["inquiry"]["status"] == "CONFIRMED" and data["inquiry"]["version"] == 3
    assert data["changed_visits"] == [{"id": str(visit_id), "version": 3}]
    assert data["inquiry"]["cash_notes"][-1]["kind"] == "REFUND_NOTE"
    missing = client.post(f"/api/v1/admin/inquiries/{uuid4()}/cash-notes", headers=headers(csrf_token, "missing"), json=payload())
    assert missing.status_code == 404 and missing.json() == {"error": {"code": "NOT_FOUND"}}
    with database.connect() as connection:
        assert connection.execute(text("SELECT status, version FROM inquiries WHERE id=:id"), {"id": planned_id}).one() == ("CONFIRMED", 3)
        assert connection.execute(text("SELECT version FROM visits WHERE id=:id"), {"id": visit_id}).scalar_one() == 3
        assert connection.execute(text("SELECT count(*) FROM cash_notes WHERE inquiry_id=:id"), {"id": planned_id}).scalar_one() == 1


def test_cash_note_correction_browser_boundary_strict_uuid_and_key_reuse(database: Engine):
    _, service_id = seed(database)
    inquiry_id = create_inquiry(database, service_id)
    client = client_for(database)
    target = f"/api/v1/admin/inquiries/{inquiry_id}/cash-notes"
    csrf_token = login(client)
    recorded = client.post(target, headers=headers(csrf_token, "correction-source"), json=payload())
    note_id = UUID(recorded.json()["data"]["inquiry"]["cash_notes"][0]["id"])
    correction_target = f"{target}/{note_id}/corrections"
    body = correction_payload(note_id)
    assert client_for(database).post(correction_target, headers={"Origin": ORIGIN, "Content-Type": "application/json", "Idempotency-Key": "missing-auth"}, json=body).status_code == 401
    assert client.post(correction_target, headers={**headers(csrf_token, "bad-origin"), "Origin": "https://untrusted.invalid"}, json=body).status_code == 403
    assert client.post(correction_target, headers={**headers("wrong", "bad-csrf")}, json=body).status_code == 403
    assert client.post(correction_target, headers={**headers(csrf_token, "bad-media"), "Content-Type": "text/plain"}, content="{}").status_code == 415
    assert client.post(correction_target, headers=headers(csrf_token, "strict"), json={**body, "extra": True}).status_code == 422
    invalid_replacement = {"kind": "RECEIPT", "amount_minor": 175_000, "occurred_at": "2026-10-01T08:00:00Z", "note": "Synthetic cash fact", "extra": True}
    nested_rejected = client.post(correction_target, headers=headers(csrf_token, "nested"), json={**body, "replacement": invalid_replacement})
    assert nested_rejected.status_code == 422 and nested_rejected.json() == {"error": {"code": "VALIDATION_ERROR"}}
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM cash_note_corrections")).scalar_one() == 0
    assert client.post(correction_target, headers={**headers(csrf_token, "strict")}, json=body).status_code == 200
    malformed = client.post(f"{target}/not-a-uuid/corrections", headers=headers(csrf_token, "malformed"), json=body)
    assert malformed.status_code == 404 and malformed.json() == {"error": {"code": "NOT_FOUND"}}


def test_cash_note_correction_null_replacement_replay_superseded_and_audit(database: Engine):
    owner_id, service_id = seed(database)
    inquiry_id = create_inquiry(database, service_id)
    client = client_for(database)
    csrf_token = login(client)
    source = f"/api/v1/admin/inquiries/{inquiry_id}/cash-notes"
    recorded = client.post(source, headers=headers(csrf_token, "source"), json=payload())
    data = recorded.json()["data"]
    note_id = UUID(data["inquiry"]["cash_notes"][0]["id"])
    target = f"{source}/{note_id}/corrections"
    request_headers = headers(csrf_token, "too-long")
    body = correction_payload(note_id)
    too_long = client.post(target, headers=request_headers, json=correction_payload(note_id, reason="x" * 501))
    assert too_long.status_code == 422
    first = client.post(target, headers=request_headers, json=body)
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    first_data = first.json()["data"]
    assert first_data["inquiry"]["version"] == 3 and first_data["inquiry"]["status"] == "NEW"
    assert first_data["inquiry"]["cash_notes"][0]["effective"] is False
    assert first_data["inquiry"]["cash_notes"][0]["correction"]["replacement_note_id"] is None
    with database.begin() as connection:
        connection.execute(text("UPDATE inquiries SET requester_name='Projection mutation' WHERE id=:id"), {"id": inquiry_id})
    replay = client.post(target, headers=request_headers, json=body)
    assert replay.status_code == 200 and replay.headers["idempotent-replay"] == "true" and replay.json() == first.json()
    mismatch = client.post(target, headers=request_headers, json=correction_payload(note_id, reason="different"))
    assert mismatch.status_code == 409 and mismatch.json() == {"error": {"code": "IDEMPOTENCY_MISMATCH"}}
    superseded = client.post(target, headers=headers(csrf_token, "second-correction"), json=correction_payload(note_id, expected_version=3))
    assert superseded.status_code == 409 and superseded.json() == {"error": {"code": "CASH_NOTE_SUPERSEDED"}}
    foreign = client.post(f"{source}/{uuid4()}/corrections", headers=headers(csrf_token, "foreign"), json=correction_payload(note_id, expected_version=3))
    assert foreign.status_code == 404 and foreign.json() == {"error": {"code": "NOT_FOUND"}}
    with database.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM change_events WHERE command_id=:id AND action='CASH_NOTE_CORRECTED'"), {"id": UUID(first_data["command_id"])}).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM cash_note_corrections WHERE original_note_id=:id"), {"id": note_id}).scalar_one() == 1
        connection.execute(text("UPDATE operation_receipts SET result_json=NULL, tombstoned_at=now() WHERE id=:id"), {"id": UUID(first_data["command_id"])})
    expired = client.post(target, headers=request_headers, json=body)
    assert expired.status_code == 410 and expired.json() == {"error": {"code": "RESULT_EXPIRED"}}


def test_cash_note_correction_replacement_and_planned_versions(database: Engine):
    owner_id, service_id = seed(database)
    inquiry_id = create_inquiry(database, service_id)
    service = OwnerCommandService(database, owner_id, hmac_secret=COMMAND_SECRET, digest_key_version=3)
    visit_id = create_and_confirm(service, inquiry_id, service_id)
    client = client_for(database)
    csrf_token = login(client)
    source = f"/api/v1/admin/inquiries/{inquiry_id}/cash-notes"
    recorded = client.post(source, headers=headers(csrf_token, "planned-source"), json=payload(inquiry_version=2, visit_versions={str(visit_id): 2}))
    note_id = UUID(recorded.json()["data"]["inquiry"]["cash_notes"][0]["id"])
    target = f"{source}/{note_id}/corrections"
    replacement = {"kind": "REFUND_NOTE", "amount_minor": 111_000, "occurred_at": "2026-10-02T08:00:00Z", "note": "Replacement fact"}
    stale = client.post(target, headers=headers(csrf_token, "planned-stale"), json=correction_payload(note_id, expected_version=2, replacement=replacement, visit_versions={str(visit_id): 2}))
    assert stale.status_code == 409 and stale.json() == {"error": {"code": "VERSION_CONFLICT"}}
    valid = client.post(target, headers=headers(csrf_token, "planned-valid"), json=correction_payload(note_id, expected_version=3, replacement=replacement, visit_versions={str(visit_id): 3}))
    assert valid.status_code == 200
    notes = valid.json()["data"]["inquiry"]["cash_notes"]
    assert any(note["kind"] == "REFUND_NOTE" and note["amount_minor"] == 111_000 for note in notes)
    assert valid.json()["data"]["changed_visits"] == [{"id": str(visit_id), "version": 4}]
    with database.connect() as connection:
        assert connection.execute(text("SELECT status, version FROM inquiries WHERE id=:id"), {"id": inquiry_id}).one() == ("CONFIRMED", 4)
        assert connection.execute(text("SELECT version FROM visits WHERE id=:id"), {"id": visit_id}).scalar_one() == 4
