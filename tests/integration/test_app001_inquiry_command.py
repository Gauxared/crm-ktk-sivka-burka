from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from sivka_burka_api.inquiries import (
    IdempotencyMismatch,
    OptionUnavailable,
    PublicInquiryCommandService,
    SubmissionAlreadyUsed,
    SubmissionExpired,
)

ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE = "sivka_app001_test"


def source_url() -> str:
    if configured := os.environ.get("DATABASE_URL"):
        return configured
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1]
    raise RuntimeError("DATABASE_URL is not configured")


@pytest.fixture()
def database(monkeypatch):
    target = make_url(source_url()).set(database=TEST_DATABASE)
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.begin() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {TEST_DATABASE} WITH (FORCE)"))
        connection.execute(text(f"CREATE DATABASE {TEST_DATABASE}"))
    admin.dispose()
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


def payload(token="synthetic-token"):
    return {
        "submission_token": token, "catalog_version": 1, "service_option_id": "ride-60",
        "requester": {"name": "Synthetic Client", "contact": {"kind": "PHONE", "value": "+70000000000"}},
        "participants_count": 2, "requested_time": {"date": "2026-10-01", "time_text": "after 14:00"},
        "experience": "BEGINNER", "comment": "Synthetic inquiry", "acquisition": {"utm_source": "test"},
    }


def seed_catalog_and_token(engine, token="synthetic-token", *, expires="now() + interval '1 hour'"):
    service_id, option_id = uuid4(), uuid4()
    secret = b"test-hmac-secret"
    from sivka_burka_api.inquiries import _digest
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO club_settings (id, timezone, location_link, visit_rules) VALUES (1, 'Asia/Bangkok', 'https://example.invalid', 'Synthetic rules')"))
        connection.execute(text("INSERT INTO services (id, code, title, description, information, active, sort_order) VALUES (:id, 'ride', 'Ride', 'Synthetic', 'Synthetic', true, 1)"), {"id": service_id})
        connection.execute(text("INSERT INTO service_options (id, service_id, code, duration_minutes, pricing_mode, price_minor, currency, active) VALUES (:id, :service, 'ride-60', 60, 'FIXED_PER_PERSON', 100, 'RUB', true)"), {"id": option_id, "service": service_id})
        issued_at = "now() - interval '2 hours'" if "- interval" in expires else "now()"
        connection.execute(text(f"INSERT INTO submission_tokens (token_digest, digest_key_version, issued_at, expires_at) VALUES (:digest, 1, {issued_at}, {expires})"), {"digest": _digest(secret, token)})
    return secret


def test_submit_is_atomic_and_completed_replay_is_read_only(database):
    secret = seed_catalog_and_token(database)
    service = PublicInquiryCommandService(database, hmac_secret=secret)
    first = service.submit(payload(), "request-key")
    replay = service.submit(payload(), "request-key")
    assert first.inquiry_id == replay.inquiry_id
    assert replay.replay is True
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM inquiries")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM operation_receipts")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM notification_jobs")).scalar_one() == 1
        assert connection.execute(text("SELECT consumed_at IS NOT NULL FROM submission_tokens")).scalar_one()


def test_same_key_mismatch_and_consumed_token_mismatch_do_not_mutate(database):
    secret = seed_catalog_and_token(database)
    service = PublicInquiryCommandService(database, hmac_secret=secret)
    service.submit(payload(), "request-key")
    with pytest.raises(IdempotencyMismatch):
        service.submit({**payload(), "comment": "different"}, "request-key")
    with pytest.raises(SubmissionAlreadyUsed):
        service.submit({**payload(), "comment": "different"}, "other-key")
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM inquiries")).scalar_one() == 1


def test_expired_token_and_invalid_option_roll_back_without_receipt(database):
    secret = seed_catalog_and_token(database, token="expired", expires="now() - interval '1 second'")
    service = PublicInquiryCommandService(database, hmac_secret=secret)
    with pytest.raises(SubmissionExpired):
        service.submit(payload("expired"), "expired-key")
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM operation_receipts")).scalar_one() == 0


def test_unknown_option_rolls_back_and_leaves_token_unused(database):
    secret = seed_catalog_and_token(database, token="unknown-option")
    service = PublicInquiryCommandService(database, hmac_secret=secret)
    with pytest.raises(OptionUnavailable):
        service.submit({**payload("unknown-option"), "service_option_id": "missing-option"}, "unknown-option-key")
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM inquiries")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM operation_receipts")).scalar_one() == 0
        assert not connection.execute(text("SELECT consumed_at IS NOT NULL FROM submission_tokens")).scalar_one()
