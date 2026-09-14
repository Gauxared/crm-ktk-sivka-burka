"""Disposable PostgreSQL coverage for the CAL-001 owner visit read boundary."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL, make_url

from sivka_burka_api.admin_sessions import AdminSessionRuntime, hash_password
from sivka_burka_api.admin_visits import AdminVisitReadRuntime
from sivka_burka_api.main import create_app


ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://crm.synthetic.invalid"


def example_url() -> URL:
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return make_url(line.removeprefix("DATABASE_URL="))
    raise RuntimeError(".env.example must provide a synthetic DATABASE_URL")


@pytest.fixture()
def database(monkeypatch: pytest.MonkeyPatch) -> Engine:
    source = example_url()
    target = source.set(database=f"sivka_admin_visits_{uuid4().hex}")
    if not source.database or target.database == source.database:
        raise AssertionError("refusing to use the template source database")
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


def seed(engine: Engine) -> tuple[UUID, UUID, UUID]:
    owner_id, service_id, option_id = uuid4(), uuid4(), uuid4()
    long_id, unknown_id, outside_id = uuid4(), uuid4(), uuid4()
    first_inquiry, closed_inquiry, unresolved_inquiry = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO services (id, code, title, description, information, active, sort_order) VALUES (:id, 'ride', 'Synthetic ride', 'd', 'i', true, 1)"), {"id": service_id})
        connection.execute(text("INSERT INTO service_options (id, service_id, code, duration_minutes, pricing_mode, price_minor, currency, active) VALUES (:id, :service_id, 'ride-60', 60, 'FIXED_PER_PERSON', 500, 'RUB', true)"), {"id": option_id, "service_id": service_id})
        connection.execute(text("INSERT INTO owner_accounts (id, login, password_hash, credentials_version) VALUES (:id, 'owner', :password, 1)"), {"id": owner_id, "password": hash_password("correct horse")})
        connection.execute(text("INSERT INTO visits (id, service_id, status, version, start_at, duration_minutes) VALUES (:id, :service, 'PLANNED', 3, :start, :duration)"), {"id": long_id, "service": service_id, "start": datetime(2026, 10, 1, 7, tzinfo=timezone.utc), "duration": 120})
        connection.execute(text("INSERT INTO visits (id, service_id, status, version, start_at) VALUES (:id, :service, 'PLANNED', 1, :start)"), {"id": unknown_id, "service": service_id, "start": datetime(2026, 10, 1, 9, tzinfo=timezone.utc)})
        connection.execute(text("INSERT INTO visits (id, service_id, status, version, start_at, duration_minutes) VALUES (:id, :service, 'PLANNED', 1, :start, 60)"), {"id": outside_id, "service": service_id, "start": datetime(2026, 10, 3, tzinfo=timezone.utc)})
        for inquiry_id, status, count, name in ((first_inquiry, "COMPLETED", 2, "Completed"), (closed_inquiry, "NEW", 3, "Closed"), (unresolved_inquiry, "NEW", 4, "Unresolved")):
            contact_id = uuid4()
            connection.execute(text("INSERT INTO contact_cards (id) VALUES (:id)"), {"id": contact_id})
            connection.execute(text("INSERT INTO inquiries (id, contact_id, source_kind, status, contact_snapshot, selection_snapshot, requester_name, contact_kind, contact_value) VALUES (:id, :contact, 'PHONE', :status, '{}'::jsonb, '{}'::jsonb, :name, 'PHONE', '+70000000000')"), {"id": inquiry_id, "contact": contact_id, "status": status, "name": name})
            connection.execute(text("INSERT INTO inquiry_terms (inquiry_id, service_option_id, participants_count, total_minor) VALUES (:inquiry, :option, :count, 1000)"), {"inquiry": inquiry_id, "option": option_id, "count": count})
        connection.execute(text("INSERT INTO visit_participations (id, inquiry_id, visit_id) VALUES (:id, :inquiry, :visit)"), {"id": uuid4(), "inquiry": first_inquiry, "visit": long_id})
        connection.execute(text("INSERT INTO visit_participations (id, inquiry_id, visit_id, closed_at, close_kind, participants_snapshot, plan_snapshot) VALUES (:id, :inquiry, :visit, now(), 'CANCELLED', '{}'::jsonb, '{}'::jsonb)"), {"id": uuid4(), "inquiry": closed_inquiry, "visit": long_id})
        connection.execute(text("INSERT INTO visit_participations (id, inquiry_id, visit_id) VALUES (:id, :inquiry, :visit)"), {"id": uuid4(), "inquiry": unresolved_inquiry, "visit": long_id})
        connection.execute(text("INSERT INTO cash_notes (id, inquiry_id, kind, amount_minor, currency, occurred_at, owner_id, note) VALUES (:id, :inquiry, 'RECEIPT', 100, 'RUB', now(), :owner, 'receipt')"), {"id": uuid4(), "inquiry": unresolved_inquiry, "owner": owner_id})
        connection.execute(text("INSERT INTO cash_notes (id, inquiry_id, kind, amount_minor, currency, occurred_at, owner_id, note) VALUES (:id, :inquiry, 'REFUND_NOTE', 200, 'RUB', now(), :owner, 'refund')"), {"id": uuid4(), "inquiry": unresolved_inquiry, "owner": owner_id})
    return long_id, unknown_id, outside_id


def client_for(engine: Engine) -> TestClient:
    return TestClient(create_app(admin_session_runtime=AdminSessionRuntime(engine, b"synthetic-session-secret", Limiter(), Lifetime(), frozenset({ORIGIN})), admin_visit_read_runtime=AdminVisitReadRuntime(engine)), base_url=ORIGIN)


def login(client: TestClient) -> None:
    assert client.post("/api/v1/admin/session", headers={"Origin": ORIGIN, "Content-Type": "application/json", "X-Requested-With": "crm"}, json={"login": "owner", "password": "correct horse"}).status_code == 200


def test_owner_calendar_intersection_privacy_pagination_and_detail(database: Engine):
    long_id, unknown_id, outside_id = seed(database)
    client = client_for(database)
    params = {"from": "2026-10-01T08:00:00Z", "to": "2026-10-01T10:00:00Z", "limit": 1}
    assert client.get("/api/v1/admin/visits", params=params).json() == {"error": {"code": "AUTH_REQUIRED"}}
    client.cookies.set("admin_session", "not-a-valid-session")
    assert client.get(f"/api/v1/admin/visits/{long_id}").json() == {"error": {"code": "AUTH_REQUIRED"}}
    login(client)
    first = client.get("/api/v1/admin/visits", params=params)
    assert first.status_code == 200
    summary = first.json()["data"]["items"][0]
    assert set(summary) == {"id", "version", "status", "service_title", "start_at", "duration_minutes", "end_at", "inquiry_count", "participants_count", "has_unresolved_inquiries"}
    assert summary["id"] == str(long_id) and summary["end_at"] == "2026-10-01T09:00:00+00:00"
    assert summary["inquiry_count"] == 2 and summary["participants_count"] == 6 and summary["has_unresolved_inquiries"] is True
    assert "requester_name" not in summary
    next_page = client.get("/api/v1/admin/visits", params={**params, "cursor": first.json()["data"]["next_cursor"]})
    assert [item["id"] for item in next_page.json()["data"]["items"]] == [str(unknown_id)]
    assert next_page.json()["data"]["items"][0]["end_at"] is None
    assert client.get("/api/v1/admin/visits", params={**params, "to": "2026-10-02T10:00:00Z", "cursor": first.json()["data"]["next_cursor"]}).status_code == 400
    for invalid in ({}, {"from": "", "to": "2026-10-01T10:00:00Z"}, {"from": "2026-10-01T10:00:00Z", "to": "2026-10-01T10:00:00Z"}, {"from": "2026-10-01T10:00:00+03:00", "to": "2026-10-01T11:00:00Z"}):
        response = client.get("/api/v1/admin/visits", params=invalid)
        assert response.status_code == 400 and response.json() == {"error": {"code": "INVALID_REQUEST"}}
    assert client.get("/api/v1/admin/visits", params={"from": "2026-10-01T00:00:00Z", "to": "2027-01-03T00:00:01Z"}).status_code == 422
    detail = client.get(f"/api/v1/admin/visits/{long_id}")
    assert detail.status_code == 200
    data = detail.json()["data"]
    assert data["id"] == str(long_id) and len(data["participations"]) == 2
    unresolved = next(item for item in data["participations"] if item["status"] == "NEW")
    assert unresolved["cash_summary"]["calculation_warning"] == "DATA_REVIEW_REQUIRED"
    assert data["allowed_commands"] == []
    assert client.get(f"/api/v1/admin/visits/{outside_id}").status_code == 200
    assert client.get(f"/api/v1/admin/visits/{uuid4()}").status_code == 404
    assert client.get("/api/v1/admin/visits/not-a-uuid").status_code == 404
