from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

from sivka_burka_api.inquiries import _digest
from sivka_burka_api.main import create_app
from sivka_burka_api.public_catalog import PublicCatalogRuntime
from sivka_burka_api.public_submission import PublicSubmissionRuntime

ROOT = Path(__file__).resolve().parents[2]
SECRET = b"synthetic-public001-hmac-key"


def disposable_url() -> tuple[URL, str]:
    """Build a random database from the explicit synthetic test server only."""
    import os

    configured = os.environ.get("SYNTHETIC_DATABASE_URL")
    if not configured:
        pytest.skip("SYNTHETIC_DATABASE_URL is required for disposable PostgreSQL tests")
    name = f"sivka_public003_{uuid4().hex}"
    return make_url(configured).set(database=name), name


class AllowingLimiter:
    def check(self, operation, request) -> None:
        return None


@pytest.fixture()
def database(monkeypatch):
    target, name = disposable_url()
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    engine = None
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("DATABASE_URL", target.render_as_string(hide_password=False))
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        engine = create_engine(target)
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        admin.dispose()
        cleanup = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
        with cleanup.begin() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        cleanup.dispose()


@pytest.fixture()
def client(database):
    runtime = PublicSubmissionRuntime(database, SECRET, AllowingLimiter())
    return TestClient(create_app(public_submission_runtime=runtime, public_catalog_runtime=PublicCatalogRuntime(database)))


def seed_catalog(engine) -> str:
    service_id, option_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO club_settings (id, timezone, location_link, visit_rules) VALUES (1, 'Asia/Bangkok', 'https://example.invalid', 'Synthetic rules')"))
        connection.execute(text("INSERT INTO services (id, code, title, description, information, active, sort_order) VALUES (:id, 'ride', 'Ride', 'Synthetic', 'Synthetic', true, 1)"), {"id": service_id})
        connection.execute(text("INSERT INTO service_options (id, service_id, code, duration_minutes, pricing_mode, price_minor, currency, active) VALUES (:id, :service, 'ride-60', 60, 'FIXED_PER_PERSON', 100, 'RUB', true)"), {"id": option_id, "service": service_id})
    return str(option_id)


def published_option_id(client) -> str:
    response = client.get("/api/v1/public/catalog")
    assert response.status_code == 200
    return response.json()["data"]["services"][0]["options"][0]["id"]


def issue(client) -> str:
    response = client.post("/api/v1/public/submission-tokens", json={})
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["data"]["expires_at"].endswith("+00:00")
    return response.json()["data"]["submission_token"]


def payload(token: str, service_option_id: str, **changes):
    body = {
        "submission_token": token,
        "catalog_version": 1,
        "service_option_id": service_option_id,
        "requester": {"name": "Synthetic Client", "contact": {"kind": "PHONE", "value": "+70000000000"}},
        "participants_count": 2,
        "requested_time": {"date": "2026-10-01", "time_text": "after 14:00"},
    }
    return {**body, **changes}


def test_issues_digest_only_and_accepts_completed_replay(database, client):
    seeded_option_id = seed_catalog(database)
    option_id = published_option_id(client)
    assert option_id == seeded_option_id
    token = issue(client)
    with database.connect() as connection:
        digest, expiry = connection.execute(text("SELECT token_digest, expires_at FROM submission_tokens")).one()
    assert digest == _digest(SECRET, token)
    assert token.encode() not in digest
    assert expiry.tzinfo is not None

    first = client.post("/api/v1/public/inquiries", json=payload(token, option_id), headers={"Idempotency-Key": "synthetic-key"})
    replay = client.post("/api/v1/public/inquiries", json=payload(token, option_id), headers={"Idempotency-Key": "synthetic-key"})
    assert first.status_code == replay.status_code == 201
    assert first.headers["cache-control"] == "no-store"
    assert first.json() == replay.json()
    assert set(first.json()["data"]) == {"receipt_id", "received", "booking_confirmed"}
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM inquiries")).scalar_one() == 1


def test_missing_or_malformed_input_and_header_are_rejected(database, client):
    option_id = seed_catalog(database)
    token = issue(client)
    assert client.post("/api/v1/public/submission-tokens", json={"unexpected": True}).status_code == 400
    assert client.post("/api/v1/public/inquiries", json=payload(token, option_id)).json() == {"error": {"code": "INVALID_REQUEST"}}
    assert client.post("/api/v1/public/inquiries", content="not-json", headers={"Idempotency-Key": "key"}).status_code == 400
    assert client.post("/api/v1/public/inquiries", json=payload(token, option_id, status="CONFIRMED"), headers={"Idempotency-Key": "key"}).status_code == 400


def test_expired_used_mismatched_and_unavailable_requests_do_not_create_duplicates(database, client):
    option_id = seed_catalog(database)
    expired = "synthetic-expired-token"
    with database.begin() as connection:
        connection.execute(text("INSERT INTO submission_tokens (token_digest, digest_key_version, issued_at, expires_at) VALUES (:digest, 1, now() - interval '2 hours', now() - interval '1 second')"), {"digest": _digest(SECRET, expired)})
    assert client.post("/api/v1/public/inquiries", json=payload(expired, option_id), headers={"Idempotency-Key": "expired-key"}).json() == {"error": {"code": "SUBMISSION_EXPIRED"}}

    token = issue(client)
    accepted = client.post("/api/v1/public/inquiries", json=payload(token, option_id), headers={"Idempotency-Key": "first-key"})
    assert accepted.status_code == 201
    assert client.post("/api/v1/public/inquiries", json=payload(token, option_id, comment="different"), headers={"Idempotency-Key": "first-key"}).json() == {"error": {"code": "IDEMPOTENCY_MISMATCH"}}
    assert client.post("/api/v1/public/inquiries", json=payload(token, option_id, comment="different"), headers={"Idempotency-Key": "other-key"}).json() == {"error": {"code": "SUBMISSION_ALREADY_USED"}}

    available_token = issue(client)
    option = client.post("/api/v1/public/inquiries", json=payload(available_token, "missing"), headers={"Idempotency-Key": "option-key"})
    assert option.status_code == 409
    assert option.json() == {"error": {"code": "OPTION_UNAVAILABLE"}}
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM inquiries")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM operation_receipts")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM submission_tokens WHERE consumed_at IS NOT NULL")).scalar_one() == 1


def test_legacy_option_code_is_unavailable_without_persistence(database, client):
    seed_catalog(database)
    token = issue(client)
    response = client.post("/api/v1/public/inquiries", json=payload(token, "ride-60"), headers={"Idempotency-Key": "legacy-code-key"})
    assert response.status_code == 409
    assert response.json() == {"error": {"code": "OPTION_UNAVAILABLE"}}
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM inquiries")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM operation_receipts")).scalar_one() == 0
        assert not connection.execute(text("SELECT consumed_at IS NOT NULL FROM submission_tokens")).scalar_one()


def test_inactive_published_option_is_unavailable_without_persistence(database, client):
    option_id = seed_catalog(database)
    with database.begin() as connection:
        connection.execute(text("UPDATE service_options SET active = false WHERE id = :id"), {"id": option_id})
    token = issue(client)
    response = client.post("/api/v1/public/inquiries", json=payload(token, option_id), headers={"Idempotency-Key": "inactive-option-key"})
    assert response.status_code == 409
    assert response.json() == {"error": {"code": "OPTION_UNAVAILABLE"}}
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM inquiries")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM operation_receipts")).scalar_one() == 0
        assert not connection.execute(text("SELECT consumed_at IS NOT NULL FROM submission_tokens")).scalar_one()
