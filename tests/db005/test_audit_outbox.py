from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE = "sivka_db005_test"


def database_url() -> str:
    if configured := os.environ.get("DATABASE_URL"):
        return configured
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.removeprefix("DATABASE_URL=")
    raise RuntimeError("DATABASE_URL is not available for DB-005 tests")


@pytest.fixture()
def upgraded_database(monkeypatch: pytest.MonkeyPatch):
    target_url = make_url(database_url()).set(database=TEST_DATABASE)
    admin_url = target_url.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {TEST_DATABASE} WITH (FORCE)"))
        connection.execute(text(f"CREATE DATABASE {TEST_DATABASE}"))
    admin_engine.dispose()
    monkeypatch.setenv("DATABASE_URL", target_url.render_as_string(hide_password=False))
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    engine = create_engine(target_url)
    try:
        yield engine
    finally:
        engine.dispose()
        cleanup = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with cleanup.connect() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS {TEST_DATABASE} WITH (FORCE)"))
        cleanup.dispose()


def seed_inquiry(connection):
    service_id, option_id, contact_id, inquiry_id, receipt_id, event_id = [uuid4() for _ in range(6)]
    connection.execute(text("INSERT INTO services (id, code, title, description, information, active, sort_order) VALUES (:id, 'ride', 'Ride', '', '', true, 1)"), {"id": service_id})
    connection.execute(text("INSERT INTO service_options (id, service_id, code, duration_minutes, pricing_mode, price_minor, currency, active) VALUES (:id, :service, 'ride-60', 60, 'FIXED_PER_PERSON', 100, 'RUB', true)"), {"id": option_id, "service": service_id})
    connection.execute(text("INSERT INTO contact_cards (id) VALUES (:id)"), {"id": contact_id})
    connection.execute(text("INSERT INTO inquiries (id, contact_id, source_kind, status, contact_snapshot, selection_snapshot, requester_name, contact_kind, contact_value) VALUES (:id, :contact, 'PHONE', 'NEW', '{}'::jsonb, '{}'::jsonb, 'Synthetic', 'PHONE', '+70000000000')"), {"id": inquiry_id, "contact": contact_id})
    connection.execute(text("INSERT INTO inquiry_terms (inquiry_id, service_option_id, participants_count, currency) VALUES (:inquiry, :option, 1, 'RUB')"), {"inquiry": inquiry_id, "option": option_id})
    connection.execute(text("INSERT INTO operation_receipts (id, scope_kind, scope_id, method, canonical_path, key_digest, payload_digest, digest_key_version, normalization_version, result_json) VALUES (:id, 'PUBLIC_FORM', 'PUBLIC_FORM', 'POST', '/api/v1/public/inquiries', :key, :payload, 1, 1, '{}'::jsonb)"), {"id": receipt_id, "key": b"key", "payload": b"payload"})
    connection.execute(text("INSERT INTO change_events (id, command_id, ordinal, actor_kind, action, changes) VALUES (:id, :receipt, 1, 'SYSTEM', 'INQUIRY_RECEIVED', '{\"after\":{\"status\":\"NEW\"}}'::jsonb)"), {"id": event_id, "receipt": receipt_id})
    return inquiry_id, receipt_id, event_id


def test_receipt_linked_event_and_job_are_persisted_without_send(upgraded_database):
    with upgraded_database.begin() as connection:
        inquiry_id, receipt_id, event_id = seed_inquiry(connection)
        connection.execute(text("INSERT INTO event_inquiries (event_id, inquiry_id) VALUES (:event, :inquiry)"), {"event": event_id, "inquiry": inquiry_id})
        connection.execute(text("INSERT INTO notification_recipient_state (recipient_ref) VALUES ('OWNER_PRIMARY_UNCONFIGURED')"))
        connection.execute(text("INSERT INTO notification_jobs (id, origin_event_id, inquiry_id, recipient_ref, status) VALUES (:id, :event, :inquiry, 'OWNER_PRIMARY_UNCONFIGURED', 'BLOCKED')"), {"id": uuid4(), "event": event_id, "inquiry": inquiry_id})
        counts = connection.execute(text("SELECT (SELECT count(*) FROM event_inquiries WHERE event_id = :event), (SELECT count(*) FROM notification_jobs WHERE origin_event_id = :event), (SELECT count(*) FROM notification_attempts)"), {"event": event_id}).one()
    assert counts == (1, 1, 0)
    assert receipt_id


def test_outbox_rejects_duplicate_job_and_invalid_lease_state(upgraded_database):
    with upgraded_database.begin() as connection:
        inquiry_id, _, event_id = seed_inquiry(connection)
        connection.execute(text("INSERT INTO notification_recipient_state (recipient_ref) VALUES ('OWNER_PRIMARY_UNCONFIGURED')"))
        connection.execute(text("INSERT INTO notification_jobs (id, origin_event_id, inquiry_id, recipient_ref, status) VALUES (:id, :event, :inquiry, 'OWNER_PRIMARY_UNCONFIGURED', 'PENDING')"), {"id": uuid4(), "event": event_id, "inquiry": inquiry_id})
    with pytest.raises(IntegrityError):
        with upgraded_database.begin() as connection:
            connection.execute(text("INSERT INTO notification_jobs (id, origin_event_id, inquiry_id, recipient_ref, status) VALUES (:id, :event, :inquiry, 'OWNER_PRIMARY_UNCONFIGURED', 'PENDING')"), {"id": uuid4(), "event": event_id, "inquiry": inquiry_id})
    with pytest.raises(IntegrityError):
        with upgraded_database.begin() as connection:
            connection.execute(text("UPDATE notification_jobs SET status = 'SENDING' WHERE origin_event_id = :event"), {"event": event_id})
