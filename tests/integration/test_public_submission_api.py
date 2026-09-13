from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from sivka_burka_api.inquiries import _digest
from sivka_burka_api.main import create_app
from sivka_burka_api.public_submission import PublicSubmissionRuntime

ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE = "sivka_public001_test"
SECRET = b"synthetic-public001-hmac-key"


def source_url() -> str:
    if configured := os.environ.get("DATABASE_URL"):
        return configured
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1]
    raise RuntimeError("DATABASE_URL is not configured")


class AllowingLimiter:
    def check(self, operation, request) -> None:
        return None


@pytest.fixture()
def database(monkeypatch):
    target = make_url(source_url()).set(database=TEST_DATABASE)
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.begin() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {TEST_DATABASE} WITH (FORCE)"))
        connection.execute(text(f"CREATE DATABASE {TEST_DATABASE}"))
    admin.dispose()
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", target.render_as_string(hide_password=False))
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    engine = create_engine(target)
    try:
        yield engine
    finally:
        engine.dispose()
        cleanup = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
        with cleanup.begin() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS {TEST_DATABASE} WITH (FORCE)"))
        cleanup.dispose()


@pytest.fixture()
def client(database):
    runtime = PublicSubmissionRuntime(database, SECRET, AllowingLimiter())
    return TestClient(create_app(public_submission_runtime=runtime))


def seed_catalog(engine) -> None:
    service_id, option_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO club_settings (id, timezone, location_link, visit_rules) VALUES (1, 'Asia/Bangkok', 'https://example.invalid', 'Synthetic rules')"))
        connection.execute(text("INSERT INTO services (id, code, title, description, information, active, sort_order) VALUES (:id, 'ride', 'Ride', 'Synthetic', 'Synthetic', true, 1)"), {"id": service_id})
        connection.execute(text("INSERT INTO service_options (id, service_id, code, duration_minutes, pricing_mode, price_minor, currency, active) VALUES (:id, :service, 'ride-60', 60, 'FIXED_PER_PERSON', 100, 'RUB', true)"), {"id": option_id, "service": service_id})


def issue(client) -> str:
    response = client.post("/api/v1/public/submission-tokens", json={})
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["data"]["expires_at"].endswith("+00:00")
    return response.json()["data"]["submission_token"]


def payload(token: str, **changes):
    body = {
        "submission_token": token,
        "catalog_version": 1,
        "service_option_id": "ride-60",
        "requester": {"name": "Synthetic Client", "contact": {"kind": "PHONE", "value": "+70000000000"}},
        "participants_count": 2,
        "requested_time": {"date": "2026-10-01", "time_text": "after 14:00"},
    }
    return {**body, **changes}


def test_issues_digest_only_and_accepts_completed_replay(database, client):
    seed_catalog(database)
    token = issue(client)
    with database.connect() as connection:
        digest, expiry = connection.execute(text("SELECT token_digest, expires_at FROM submission_tokens")).one()
    assert digest == _digest(SECRET, token)
    assert token.encode() not in digest
    assert expiry.tzinfo is not None

    first = client.post("/api/v1/public/inquiries", json=payload(token), headers={"Idempotency-Key": "synthetic-key"})
    replay = client.post("/api/v1/public/inquiries", json=payload(token), headers={"Idempotency-Key": "synthetic-key"})
    assert first.status_code == replay.status_code == 201
    assert first.headers["cache-control"] == "no-store"
    assert first.json() == replay.json()
    assert set(first.json()["data"]) == {"receipt_id", "received", "booking_confirmed"}
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM inquiries")).scalar_one() == 1


def test_missing_or_malformed_input_and_header_are_rejected(database, client):
    seed_catalog(database)
    token = issue(client)
    assert client.post("/api/v1/public/submission-tokens", json={"unexpected": True}).status_code == 400
    assert client.post("/api/v1/public/inquiries", json=payload(token)).json() == {"error": {"code": "INVALID_REQUEST"}}
    assert client.post("/api/v1/public/inquiries", content="not-json", headers={"Idempotency-Key": "key"}).status_code == 400
    assert client.post("/api/v1/public/inquiries", json=payload(token, status="CONFIRMED"), headers={"Idempotency-Key": "key"}).status_code == 400


def test_expired_used_mismatched_and_unavailable_requests_do_not_create_duplicates(database, client):
    seed_catalog(database)
    expired = "synthetic-expired-token"
    with database.begin() as connection:
        connection.execute(text("INSERT INTO submission_tokens (token_digest, digest_key_version, issued_at, expires_at) VALUES (:digest, 1, now() - interval '2 hours', now() - interval '1 second')"), {"digest": _digest(SECRET, expired)})
    assert client.post("/api/v1/public/inquiries", json=payload(expired), headers={"Idempotency-Key": "expired-key"}).json() == {"error": {"code": "SUBMISSION_EXPIRED"}}

    token = issue(client)
    accepted = client.post("/api/v1/public/inquiries", json=payload(token), headers={"Idempotency-Key": "first-key"})
    assert accepted.status_code == 201
    assert client.post("/api/v1/public/inquiries", json=payload(token, comment="different"), headers={"Idempotency-Key": "first-key"}).json() == {"error": {"code": "IDEMPOTENCY_MISMATCH"}}
    assert client.post("/api/v1/public/inquiries", json=payload(token, comment="different"), headers={"Idempotency-Key": "other-key"}).json() == {"error": {"code": "SUBMISSION_ALREADY_USED"}}

    available_token = issue(client)
    option = client.post("/api/v1/public/inquiries", json=payload(available_token, service_option_id="missing"), headers={"Idempotency-Key": "option-key"})
    assert option.status_code == 409
    assert option.json() == {"error": {"code": "OPTION_UNAVAILABLE"}}
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM inquiries")).scalar_one() == 1
