"""Disposable PostgreSQL evidence for the channel-draft adapter.

The test URL comes only from the committed synthetic example, never a .env file
or a configured runtime database.
"""
from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from apps.api.sivka_burka_api.channel_draft_persistence import PostgresChannelDraftPort
from apps.api.sivka_burka_api.channel_gateway import ChannelContext, ChannelGateway, GatewayError, Platform


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE = "sivka_bot003_test"


def _synthetic_url() -> str:
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.removeprefix("DATABASE_URL=")
    raise RuntimeError("synthetic DATABASE_URL is absent from .env.example")


@pytest.fixture()
def database(monkeypatch):
    target = make_url(_synthetic_url()).set(database=TEST_DATABASE)
    admin = target.set(database="postgres")
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {TEST_DATABASE} WITH (FORCE)"))
        connection.execute(text(f"CREATE DATABASE {TEST_DATABASE}"))
    admin_engine.dispose()
    monkeypatch.setenv("DATABASE_URL", target.render_as_string(hide_password=False))
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    engine = create_engine(target)
    try:
        yield engine
    finally:
        engine.dispose()
        cleanup = create_engine(admin, isolation_level="AUTOCOMMIT")
        with cleanup.connect() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS {TEST_DATABASE} WITH (FORCE)"))
        cleanup.dispose()


def context(platform=Platform.TELEGRAM, event="event-1"):
    return ChannelContext(platform, "integration", "sender", "conversation", event)


def port(database):
    return PostgresChannelDraftPort(database, b"bot003-test-secret", draft_ttl=__import__("datetime").timedelta(minutes=30))


def seed_identity(database, channel_context):
    with database.begin() as connection:
        contact_id, identity_id = uuid4(), uuid4()
        connection.execute(text("INSERT INTO contact_cards (id) VALUES (:id)"), {"id": contact_id})
        connection.execute(text("""INSERT INTO channel_identities
            (id, contact_id, platform, integration_id, external_sender_id)
            VALUES (:id, :contact_id, :platform, :integration, :sender)"""), {
            "id": identity_id, "contact_id": contact_id, "platform": channel_context.platform.value,
            "integration": channel_context.integration_id, "sender": channel_context.external_sender_id,
        })
    return identity_id


def test_create_read_update_reset_and_replay_are_atomic(database):
    c = context(); seed_identity(database, c)
    gateway = ChannelGateway(port(database))
    first = gateway.save_draft(c, expected_version=0, answers={"name": "Nina"}, step="CONTACT", event_id=c.event_id)
    assert first.version == 1
    assert gateway.read_draft(c).draft.answers == {"name": "Nina"}
    replay = gateway.save_draft(c, expected_version=0, answers={"name": "Nina"}, step="CONTACT", event_id=c.event_id)
    assert replay == first
    updated_context = context(event="event-2")
    updated = gateway.save_draft(updated_context, expected_version=1, answers={"name": "Nina", "guests": 2}, step="SERVICE", event_id="event-2")
    assert (updated.version, updated.step) == (2, "SERVICE")
    reset_context = context(event="event-3")
    reset = gateway.reset_draft(reset_context, expected_version=2, event_id="event-3")
    assert (reset.reset, reset.version) == (True, 3)
    assert gateway.read_draft(reset_context).draft is None
    assert gateway.reset_draft(reset_context, expected_version=2, event_id="event-3") == reset
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM channel_events")).scalar_one() == 3
        assert connection.execute(text("SELECT count(*) FROM operation_receipts")).scalar_one() == 3


def test_read_catalog_is_public_equivalent_filtered_and_read_only(database):
    service_id, inactive_service, fixed_id, negotiated_id, inactive_option, inactive_service_option = (uuid4() for _ in range(6))
    with database.begin() as connection:
        connection.execute(text("INSERT INTO club_settings (id, timezone, location_link, visit_rules) VALUES (1, 'Asia/Bangkok', 'https://example.invalid/location', 'Synthetic rules')"))
        connection.execute(text("""INSERT INTO services (id, code, title, description, information, active, sort_order)
            VALUES (:id, 'ride', 'Ride', 'Description', 'Information', true, 1),
                   (:inactive, 'hidden', 'Hidden', 'Hidden', 'Hidden', false, 2)"""), {"id": service_id, "inactive": inactive_service})
        connection.execute(text("""INSERT INTO service_options (id, service_id, code, duration_minutes, pricing_mode, price_minor, currency, active)
            VALUES (:fixed, :service, 'fixed', 60, 'FIXED_PER_PERSON', 12500, 'RUB', true),
                   (:negotiated, :service, 'negotiated', NULL, 'NEGOTIATED', NULL, 'RUB', true),
                   (:hidden, :service, 'inactive-option', 1, 'FIXED_PER_PERSON', 1, 'RUB', false),
                   (:inactive_service_option, :inactive, 'hidden', 1, 'FIXED_PER_PERSON', 1, 'RUB', true)"""), {"fixed": fixed_id, "negotiated": negotiated_id, "hidden": inactive_option, "inactive_service_option": inactive_service_option, "service": service_id, "inactive": inactive_service})
    tables = ("contact_cards", "channel_identities", "conversation_drafts", "inquiries", "inquiry_terms", "operation_receipts", "receipt_inquiries", "channel_events", "change_events", "event_inquiries", "notification_jobs", "notification_recipient_state")
    def snapshot():
        with database.connect() as connection:
            return {table: connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() for table in tables}
    before = snapshot()
    gateway = ChannelGateway(port(database))
    telegram = gateway.read_catalog(context(Platform.TELEGRAM)).catalog
    vk = gateway.read_catalog(context(Platform.VK)).catalog
    assert telegram == vk
    assert set(telegram) == {"catalog_version", "club_timezone", "contact_info", "location_link", "visit_rules", "services"}
    expected_options = [
        {"id": str(fixed_id), "duration_minutes": 60, "pricing_mode": "FIXED_PER_PERSON", "price_minor": 12500, "currency": "RUB"},
        {"id": str(negotiated_id), "duration_minutes": None, "pricing_mode": "NEGOTIATED", "price_minor": None, "currency": "RUB"},
    ]
    expected_options.sort(key=lambda option: UUID(option["id"]))
    assert telegram["services"] == [{"id": str(service_id), "title": "Ride", "description": "Description", "information": "Information", "options": expected_options}]
    after = snapshot()
    assert after == before


def test_read_catalog_missing_state_is_typed_and_programming_errors_are_visible(database, monkeypatch):
    gateway = ChannelGateway(port(database))
    with pytest.raises(GatewayError) as unavailable:
        gateway.read_catalog(context())
    assert unavailable.value.code == "CATALOG_UNAVAILABLE"
    monkeypatch.setattr("apps.api.sivka_burka_api.channel_draft_persistence.PublicCatalogRuntime.read", lambda self: (_ for _ in ()).throw(RuntimeError("bug")))
    with pytest.raises(RuntimeError, match="bug"):
        gateway.read_catalog(context(Platform.VK))


def test_conflicts_mismatch_and_rejects_do_not_mutate(database):
    c = context(); seed_identity(database, c); gateway = ChannelGateway(port(database))
    gateway.save_draft(c, expected_version=0, answers={"name": "Nina"}, step="CONTACT", event_id="event-1")
    with pytest.raises(GatewayError) as stale:
        gateway.save_draft(context(event="event-2"), expected_version=0, answers={}, step="SERVICE", event_id="event-2")
    assert stale.value.code == "VERSION_CONFLICT"
    with pytest.raises(GatewayError) as mismatch:
        gateway.save_draft(c, expected_version=0, answers={"name": "Other"}, step="CONTACT", event_id="event-1")
    assert mismatch.value.code == "IDEMPOTENCY_MISMATCH"
    with pytest.raises(GatewayError):
        gateway.save_draft(context(event="event-3"), expected_version=1, answers={}, step="X", event_id="other")
    with database.connect() as connection:
        assert connection.execute(text("SELECT version, answers FROM conversation_drafts")).one() == (1, {"name": "Nina"})
        assert connection.execute(text("SELECT count(*) FROM channel_events")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM operation_receipts")).scalar_one() == 1


def test_platform_namespaces_and_existing_identity_boundary(database):
    telegram = context(Platform.TELEGRAM, "same-event")
    vk = context(Platform.VK, "same-event")
    seed_identity(database, telegram); seed_identity(database, vk)
    adapter = port(database)
    assert adapter.save_draft(telegram, 0, {"a": 1}, "CONTACT", "same-event").version == 1
    assert adapter.save_draft(vk, 0, {"a": 1}, "CONTACT", "same-event").version == 1
    unknown = ChannelGateway(adapter)
    with pytest.raises(GatewayError) as unknown_identity:
        unknown.save_draft(ChannelContext(Platform.VK, "integration", "other", "conversation", "new"), expected_version=0, answers={}, step="X", event_id="new")
    assert unknown_identity.value.code == "CHANNEL_IDENTITY_NOT_FOUND"
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM channel_identities")).scalar_one() == 2
        assert connection.execute(text("SELECT count(*) FROM channel_events")).scalar_one() == 2


def test_expiry_closes_old_draft_and_old_version_cannot_revive_it(database):
    c = context(); seed_identity(database, c); gateway = ChannelGateway(port(database))
    gateway.save_draft(c, expected_version=0, answers={"a": 1}, step="CONTACT", event_id="event-1")
    with database.begin() as connection:
        connection.execute(text("UPDATE conversation_drafts SET expires_at=now() - interval '1 second'"))
    assert gateway.read_draft(context(event="read")).draft is None
    with pytest.raises(GatewayError) as stale:
        gateway.save_draft(context(event="event-2"), expected_version=1, answers={"a": 2}, step="SERVICE", event_id="event-2")
    assert stale.value.code == "VERSION_CONFLICT"
    created = gateway.save_draft(context(event="event-3"), expected_version=0, answers={"a": 3}, step="SERVICE", event_id="event-3")
    assert created.version == 1
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM conversation_drafts WHERE closed_at IS NULL")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM channel_events")).scalar_one() == 2


def test_markers_contain_only_digests_and_database_enforces_one_open_draft(database):
    c = context(event="secret-event"); identity_id = seed_identity(database, c); gateway = ChannelGateway(port(database))
    gateway.save_draft(c, expected_version=0, answers={"private": "Nina"}, step="CONTACT", event_id="secret-event")
    with database.connect() as connection:
        event = connection.execute(text("SELECT event_key_digest, dialog_key_digest FROM channel_events")).one()
        receipt = connection.execute(text("SELECT key_digest, payload_digest FROM operation_receipts")).one()
        assert all(isinstance(value, bytes) and b"Nina" not in value and b"secret-event" not in value for value in (*event, *receipt))
    with pytest.raises(IntegrityError, match="one_open_conversation_draft"):
        with database.begin() as connection:
            connection.execute(text("""INSERT INTO conversation_drafts
                (id, channel_identity_id, conversation_id, version, answers, step, expires_at)
                VALUES (:id, :identity_id, 'conversation', 1, '{}'::jsonb, 'OTHER', now() + interval '1 hour')"""),
                {"id": uuid4(), "identity_id": identity_id})
