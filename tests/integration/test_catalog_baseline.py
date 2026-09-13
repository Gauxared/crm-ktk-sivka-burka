"""PostgreSQL-only checks for the catalog and inquiry Alembic baselines."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE = "sivka_db001_test"


def synthetic_database_url() -> str:
    if configured := os.environ.get("DATABASE_URL"):
        return configured
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.removeprefix("DATABASE_URL=")
    raise RuntimeError("DATABASE_URL is not available for integration tests")


def migration_config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def disposable_url() -> URL:
    return make_url(synthetic_database_url()).set(database=TEST_DATABASE)


@pytest.fixture()
def upgraded_database(monkeypatch: pytest.MonkeyPatch):
    target_url = disposable_url()
    admin_url = target_url.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {TEST_DATABASE} WITH (FORCE)"))
        connection.execute(text(f"CREATE DATABASE {TEST_DATABASE}"))
    admin_engine.dispose()

    monkeypatch.setenv("DATABASE_URL", target_url.render_as_string(hide_password=False))
    command.upgrade(migration_config(), "head")
    engine = create_engine(target_url)
    try:
        yield engine
    finally:
        engine.dispose()
        cleanup_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with cleanup_engine.connect() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS {TEST_DATABASE} WITH (FORCE)"))
        cleanup_engine.dispose()


def test_baseline_creates_required_tables_and_single_guard(upgraded_database):
    with upgraded_database.connect() as connection:
        tables = connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
        ).scalars().all()
        guard_rows = connection.execute(text("SELECT id FROM business_write_guard")).scalars().all()

    assert {"business_write_guard", "club_settings", "services", "service_options"} <= set(tables)
    assert guard_rows == [1]


def test_catalog_constraints_reject_invalid_price_models_and_guard_duplicates(upgraded_database):
    service_id = uuid4()
    option_id = uuid4()
    with upgraded_database.begin() as connection:
        connection.execute(
            text("""INSERT INTO services (id, code, title, description, information, active, sort_order)
                    VALUES (:id, 'group-ride', 'Group ride', '', '', true, 10)"""),
            {"id": service_id},
        )
        connection.execute(
            text("""INSERT INTO service_options
                    (id, service_id, code, duration_minutes, pricing_mode, price_minor, currency, active)
                    VALUES (:id, :service_id, 'group-60', 60, 'FIXED_PER_PERSON', 350000, 'RUB', true)"""),
            {"id": option_id, "service_id": service_id},
        )

    with pytest.raises(IntegrityError):
        with upgraded_database.begin() as connection:
            connection.execute(text("INSERT INTO business_write_guard (id) VALUES (1)"))

    with pytest.raises(IntegrityError):
        with upgraded_database.begin() as connection:
            connection.execute(
                text("""INSERT INTO service_options
                        (id, service_id, code, duration_minutes, pricing_mode, price_minor, currency, active)
                        VALUES (:id, :service_id, 'broken-fixed', 30, 'FIXED_PER_PERSON', NULL, 'RUB', true)"""),
                {"id": uuid4(), "service_id": service_id},
            )


def test_inquiries_keep_contact_snapshot_and_reject_invalid_terms(upgraded_database):
    service_id = uuid4()
    option_id = uuid4()
    contact_id = uuid4()
    identity_id = uuid4()
    inquiry_id = uuid4()
    with upgraded_database.begin() as connection:
        connection.execute(
            text("""INSERT INTO services (id, code, title, description, information, active, sort_order)
                    VALUES (:id, 'individual-ride', 'Individual ride', '', '', true, 20)"""),
            {"id": service_id},
        )
        connection.execute(
            text("""INSERT INTO service_options
                    (id, service_id, code, duration_minutes, pricing_mode, price_minor, currency, active)
                    VALUES (:id, :service_id, 'individual-60', 60, 'FIXED_PER_PERSON', 500000, 'RUB', true)"""),
            {"id": option_id, "service_id": service_id},
        )
        connection.execute(text("INSERT INTO contact_cards (id) VALUES (:id)"), {"id": contact_id})
        connection.execute(
            text("""INSERT INTO channel_identities (id, contact_id, platform, integration_id, external_sender_id)
                    VALUES (:id, :contact_id, 'TELEGRAM', 'test-bot', 'sender-1')"""),
            {"id": identity_id, "contact_id": contact_id},
        )
        connection.execute(
            text("""INSERT INTO inquiries
                    (id, contact_id, channel_identity_id, source_kind, status, contact_snapshot,
                     selection_snapshot, requester_name, contact_kind, contact_value, experience)
                    VALUES (:id, :contact_id, :identity_id, 'TELEGRAM', 'NEW',
                            '{"name":"Test client"}'::jsonb, '{"option":"individual-60"}'::jsonb,
                            'Test client', 'TELEGRAM', 'sender-1', 'UNKNOWN')"""),
            {"id": inquiry_id, "contact_id": contact_id, "identity_id": identity_id},
        )
        connection.execute(
            text("""INSERT INTO inquiry_terms
                    (inquiry_id, service_option_id, participants_count, duration_minutes,
                     total_minor, expected_prepayment_minor, currency, note)
                    VALUES (:inquiry_id, :option_id, 1, 60, NULL, NULL, 'RUB', '')"""),
            {"inquiry_id": inquiry_id, "option_id": option_id},
        )

    with upgraded_database.connect() as connection:
        row = connection.execute(
            text("SELECT status, contact_snapshot->>'name', total_minor FROM inquiries JOIN inquiry_terms ON inquiry_terms.inquiry_id = inquiries.id WHERE inquiries.id = :id"),
            {"id": inquiry_id},
        ).one()
    assert row == ("NEW", "Test client", None)

    with pytest.raises(IntegrityError):
        with upgraded_database.begin() as connection:
            connection.execute(
                text("""INSERT INTO channel_identities (id, contact_id, platform, integration_id, external_sender_id)
                        VALUES (:id, :contact_id, 'TELEGRAM', 'test-bot', 'sender-1')"""),
                {"id": uuid4(), "contact_id": contact_id},
            )

    with pytest.raises(IntegrityError):
        with upgraded_database.begin() as connection:
            connection.execute(
                text("""INSERT INTO inquiries
                        (id, contact_id, source_kind, status, contact_snapshot, selection_snapshot,
                         requester_name, contact_kind, contact_value)
                        VALUES (:id, :contact_id, 'EMAIL', 'NEW', '{}'::jsonb, '{}'::jsonb,
                                'Test client', 'PHONE', '+70000000000')"""),
                {"id": uuid4(), "contact_id": contact_id},
            )

    invalid_participants_inquiry_id = uuid4()
    invalid_money_inquiry_id = uuid4()
    with upgraded_database.begin() as connection:
        for new_inquiry_id in (invalid_participants_inquiry_id, invalid_money_inquiry_id):
            connection.execute(
                text("""INSERT INTO inquiries
                        (id, contact_id, source_kind, status, contact_snapshot, selection_snapshot,
                         requester_name, contact_kind, contact_value)
                        VALUES (:id, :contact_id, 'PHONE', 'NEW', '{}'::jsonb, '{}'::jsonb,
                                'Test client', 'PHONE', '+70000000000')"""),
                {"id": new_inquiry_id, "contact_id": contact_id},
            )

    with pytest.raises(IntegrityError, match="inquiry_terms_participants_positive"):
        with upgraded_database.begin() as connection:
            connection.execute(
                text("""INSERT INTO inquiry_terms
                        (inquiry_id, service_option_id, participants_count, currency)
                        VALUES (:inquiry_id, :option_id, 0, 'RUB')"""),
                {"inquiry_id": invalid_participants_inquiry_id, "option_id": option_id},
            )

    with pytest.raises(IntegrityError, match="inquiry_terms_total_range"):
        with upgraded_database.begin() as connection:
            connection.execute(
                text("""INSERT INTO inquiry_terms
                        (inquiry_id, service_option_id, participants_count, total_minor, currency)
                        VALUES (:inquiry_id, :option_id, 1, -1, 'RUB')"""),
                {"inquiry_id": invalid_money_inquiry_id, "option_id": option_id},
            )

    with pytest.raises(IntegrityError):
        with upgraded_database.begin() as connection:
            connection.execute(
                text("""INSERT INTO service_options
                        (id, service_id, code, duration_minutes, pricing_mode, price_minor, currency, active)
                        VALUES (:id, :service_id, 'broken-negotiated', NULL, 'NEGOTIATED', 100, 'RUB', true)"""),
                {"id": uuid4(), "service_id": service_id},
            )
