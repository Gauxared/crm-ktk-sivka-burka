"""Disposable PostgreSQL coverage for the ADMIN-001 owner read boundary."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL, make_url

from sivka_burka_api.admin_inquiries import AdminInquiryReadRuntime
from sivka_burka_api.admin_sessions import AdminSessionRuntime, hash_password
from sivka_burka_api.inquiries import PublicInquiryCommandService, _digest
from sivka_burka_api.main import create_app


ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://crm.synthetic.invalid"


def example_url() -> URL:
    """Read only the committed synthetic template, never a developer .env."""
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return make_url(line.removeprefix("DATABASE_URL="))
    raise RuntimeError(".env.example must provide a synthetic DATABASE_URL")


@pytest.fixture()
def database(monkeypatch: pytest.MonkeyPatch) -> Engine:
    source = example_url()
    target = source.set(database=f"sivka_admin_inquiries_{uuid4().hex}")
    if not source.database or target.database == source.database:
        raise AssertionError("refusing to use the .env.example source database")
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


def payload(token: str, service_option_id: UUID) -> dict[str, object]:
    return {
        "submission_token": token,
        "catalog_version": 1,
        "service_option_id": str(service_option_id),
        "requester": {"name": f"Synthetic {token}", "contact": {"kind": "PHONE", "value": "+70000000000"}},
        "participants_count": 2,
        "requested_time": {"date": "2026-10-01", "time_text": "after 14:00"},
        "experience": "BEGINNER",
        "comment": f"private comment {token}",
        "acquisition": {"utm_source": "synthetic"},
    }


def seed_inquiry(engine: Engine, secret: bytes, service_option_id: UUID, token: str, *, received_at: datetime, status: str, channel: str) -> UUID:
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO submission_tokens (token_digest, digest_key_version, issued_at, expires_at) VALUES (:digest, 1, now(), now() + interval '1 hour')"),
            {"digest": _digest(secret, token)},
        )
    inquiry_id = PublicInquiryCommandService(engine, hmac_secret=secret).submit(payload(token, service_option_id), f"key-{token}").inquiry_id
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE inquiries SET received_at=:received_at, status=:status, source_kind=:channel WHERE id=:id"),
            {"received_at": received_at, "status": status, "channel": channel, "id": inquiry_id},
        )
    return inquiry_id


def seed_read_model(engine: Engine) -> tuple[UUID, UUID]:
    secret = b"synthetic-inquiry-secret"
    owner_id = uuid4()
    service_id, option_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO club_settings (id, timezone, location_link, visit_rules) VALUES (1, 'Asia/Bangkok', 'https://example.invalid', 'Synthetic rules')"))
        connection.execute(text("INSERT INTO services (id, code, title, description, information, active, sort_order) VALUES (:id, 'synthetic-ride', 'Synthetic ride', 'Synthetic', 'Synthetic', true, 1)"), {"id": service_id})
        connection.execute(text("INSERT INTO service_options (id, service_id, code, duration_minutes, pricing_mode, price_minor, currency, active) VALUES (:id, :service_id, 'synthetic-ride', 60, 'FIXED_PER_PERSON', 500, 'RUB', true)"), {"id": option_id, "service_id": service_id})
        connection.execute(text("INSERT INTO owner_accounts (id, login, password_hash, credentials_version) VALUES (:id, 'owner', :password_hash, 1)"), {"id": owner_id, "password_hash": hash_password("correct horse")})

    old = seed_inquiry(engine, secret, option_id, "old", received_at=datetime(2026, 1, 1, tzinfo=timezone.utc), status="NEW", channel="PHONE")
    detail = seed_inquiry(engine, secret, option_id, "detail", received_at=datetime(2026, 1, 3, tzinfo=timezone.utc), status="NEW", channel="PHONE")
    scheduled = seed_inquiry(engine, secret, option_id, "scheduled", received_at=datetime(2026, 1, 4, tzinfo=timezone.utc), status="NEGOTIATING", channel="TELEGRAM")
    original, replacement, refund, correction, visit, participation = (uuid4() for _ in range(6))
    with engine.begin() as connection:
        connection.execute(text("UPDATE inquiries SET contact_snapshot=CAST(:contact AS jsonb), selection_snapshot=CAST(:selection AS jsonb), acquisition=CAST(:acquisition AS jsonb), owner_note='private owner note' WHERE id=:id"), {"id": detail, "contact": json.dumps({"name": "Synthetic detail", "contact": {"kind": "PHONE", "value": "+70000000000"}}), "selection": json.dumps({"option_code": "synthetic-ride", "price_minor": 500}), "acquisition": json.dumps({"utm_source": "synthetic", "campaign": "detail"})})
        connection.execute(text("INSERT INTO visits (id, service_id, status, version, start_at) VALUES (:id, :service_id, 'PLANNED', 1, '2026-10-01T08:00:00+00:00')"), {"id": visit, "service_id": service_id})
        connection.execute(text("INSERT INTO visit_participations (id, inquiry_id, visit_id, joined_at) VALUES (:id, :inquiry_id, :visit_id, '2026-01-04T00:00:00+00:00')"), {"id": participation, "inquiry_id": scheduled, "visit_id": visit})
        notes = [
            {"id": original, "kind": "RECEIPT", "amount": 1000, "note": "mistaken receipt"},
            {"id": replacement, "kind": "RECEIPT", "amount": 700, "note": "corrected receipt"},
            {"id": refund, "kind": "REFUND_NOTE", "amount": 900, "note": "refund recorded"},
        ]
        for note in notes:
            connection.execute(text("INSERT INTO cash_notes (id, inquiry_id, kind, amount_minor, currency, occurred_at, recorded_at, owner_id, note) VALUES (:id, :inquiry_id, :kind, :amount, 'RUB', '2026-01-05T00:00:00+00:00', '2026-01-05T00:00:00+00:00', :owner_id, :note)"), {**note, "inquiry_id": detail, "owner_id": owner_id})
        connection.execute(text("INSERT INTO cash_note_corrections (id, inquiry_id, original_note_id, replacement_note_id, reason, recorded_at, owner_id) VALUES (:id, :inquiry_id, :original, :replacement, 'Synthetic correction', '2026-01-06T00:00:00+00:00', :owner_id)"), {"id": correction, "inquiry_id": detail, "original": original, "replacement": replacement, "owner_id": owner_id})
        connection.execute(text("UPDATE notification_jobs SET status='RETRY_WAIT', attempt_count=2, last_error_code='SYNTHETIC_TEMPORARY_FAILURE' WHERE inquiry_id=:id"), {"id": detail})
    return detail, scheduled


def client_for(engine: Engine) -> TestClient:
    session_runtime = AdminSessionRuntime(engine, b"synthetic-session-secret", Limiter(), Lifetime(), frozenset({ORIGIN}))
    return TestClient(create_app(admin_session_runtime=session_runtime, admin_inquiry_read_runtime=AdminInquiryReadRuntime(engine)), base_url=ORIGIN)


def login(client: TestClient) -> None:
    response = client.post("/api/v1/admin/session", headers={"Origin": ORIGIN, "Content-Type": "application/json", "X-Requested-With": "crm"}, json={"login": "owner", "password": "correct horse"})
    assert response.status_code == 200


def test_owner_inquiry_list_and_detail_use_a_disposable_postgres_read_model(database: Engine):
    detail_id, scheduled_id = seed_read_model(database)
    client = client_for(database)

    for path in ("/api/v1/admin/inquiries", f"/api/v1/admin/inquiries/{detail_id}"):
        response = client.get(path)
        assert response.status_code == 401
        assert response.json() == {"error": {"code": "AUTH_REQUIRED"}}
    client.cookies.set("admin_session", "not-a-valid-session")
    assert client.get("/api/v1/admin/inquiries").json() == {"error": {"code": "AUTH_REQUIRED"}}

    login(client)
    first = client.get("/api/v1/admin/inquiries", params={"status": "NEW", "has_visit": "false", "channel": "PHONE", "limit": 1})
    assert first.status_code == 200
    first_data = first.json()["data"]
    summary_keys = {"id", "version", "status", "requester_name", "service_title", "participants_count", "requested_time", "visit_id", "agreed_start_at", "received_at", "channel", "cash_summary"}
    assert set(first_data["items"][0]) == summary_keys
    assert "contact_snapshot" not in first_data["items"][0]
    assert "comment" not in first_data["items"][0]
    assert first_data["items"][0]["id"] == str(detail_id)
    assert first_data["next_cursor"]
    second = client.get("/api/v1/admin/inquiries", params={"status": "NEW", "has_visit": "false", "channel": "PHONE", "limit": 1, "cursor": first_data["next_cursor"]})
    assert second.status_code == 200
    assert [item["id"] for item in second.json()["data"]["items"]] != [str(detail_id)]
    assert client.get("/api/v1/admin/inquiries", params={"status": "NEGOTIATING", "has_visit": "false", "channel": "PHONE", "cursor": first_data["next_cursor"]}).status_code == 400
    scheduled = client.get("/api/v1/admin/inquiries", params={"has_visit": "true"})
    assert [item["id"] for item in scheduled.json()["data"]["items"]] == [str(scheduled_id)]

    detail = client.get(f"/api/v1/admin/inquiries/{detail_id}")
    assert detail.status_code == 200
    data = detail.json()["data"]
    assert set(data) == summary_keys | {"contact_snapshot", "current_contact", "selected_option_snapshot", "agreed_terms", "comment", "owner_note", "acquisition", "participation", "cash_notes", "notification_summary", "allowed_commands"}
    assert data["contact_snapshot"]["contact"]["value"] == "+70000000000"
    assert data["current_contact"] == {"kind": "PHONE", "value": "+70000000000"}
    assert data["selected_option_snapshot"]["option_code"] == "synthetic-ride"
    assert data["acquisition"]["campaign"] == "detail"
    assert data["comment"] == "private comment detail"
    assert data["owner_note"] == "private owner note"
    assert data["cash_summary"] == {"received_minor": 700, "returned_minor": 900, "net_received_minor": -200, "total_minor": 1000, "balance_minor": 1200, "calculation_warning": "DATA_REVIEW_REQUIRED"}
    ineffective = next(note for note in data["cash_notes"] if note["note"] == "mistaken receipt")
    assert ineffective["effective"] is False
    assert ineffective["correction"]["replacement_note_id"]
    assert len(data["notification_summary"]) == 1
    notification = data["notification_summary"][0]
    assert set(notification) == {"job_id", "version", "status", "attempt_count", "next_attempt_at", "delivered_at", "safe_error_code"}
    assert notification["version"] == 1
    assert notification["status"] == "RETRY_WAIT"
    assert notification["attempt_count"] == 2
    assert notification["delivered_at"] is None
    assert notification["safe_error_code"] == "SYNTHETIC_TEMPORARY_FAILURE"
    assert data["allowed_commands"] == []
    scheduled_detail = client.get(f"/api/v1/admin/inquiries/{scheduled_id}")
    assert scheduled_detail.status_code == 200
    assert scheduled_detail.json()["data"]["participation"]["visit_id"]
    assert scheduled_detail.json()["data"]["participation"]["visit_status"] == "PLANNED"
    missing = client.get(f"/api/v1/admin/inquiries/{uuid4()}")
    assert missing.status_code == 404
    assert missing.json() == {"error": {"code": "NOT_FOUND"}}
    assert client.get("/api/v1/admin/inquiries/not-a-uuid").status_code == 404
