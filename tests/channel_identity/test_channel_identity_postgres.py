"""Disposable PostgreSQL evidence for trusted channel identity provisioning."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from apps.api.sivka_burka_api.channel_gateway import ChannelContext, Platform
from apps.api.sivka_burka_api.channel_identity import (
    ChannelIdentityPersistenceError,
    ChannelIdentityService,
    InvalidChannelContext,
)


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE = "sivka_bot008_test"


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


def context(platform=Platform.TELEGRAM, integration="integration", sender="sender", conversation="conversation", event="event"):
    return ChannelContext(platform, integration, sender, conversation, event)


def counts(database):
    with database.connect() as connection:
        return tuple(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() for table in (
            "contact_cards", "channel_identities", "inquiries", "conversation_drafts", "channel_events",
        ))


def test_first_create_and_exact_replay_are_contact_backed_and_side_effect_free(database):
    service = ChannelIdentityService(database)
    before = counts(database)
    first = service.ensure_identity(context())
    replay = service.ensure_identity(context(event="different-event", conversation="different-conversation"))

    assert first.created is True
    assert replay == type(first)(first.identity_id, first.contact_id, False)
    assert counts(database) == tuple(value + delta for value, delta in zip(before, (1, 1, 0, 0, 0)))
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM channel_identities WHERE contact_id = :id"), {"id": first.contact_id}).scalar_one() == 1


def test_platform_integration_and_sender_are_separate_namespaces(database):
    service = ChannelIdentityService(database)
    identities = [
        service.ensure_identity(context()),
        service.ensure_identity(context(Platform.VK)),
        service.ensure_identity(context(integration="other")),
        service.ensure_identity(context(sender="other")),
    ]
    assert len({item.identity_id for item in identities}) == 4
    assert len({item.contact_id for item in identities}) == 4


def test_invalid_context_is_rejected_without_database_access(database):
    service = ChannelIdentityService(database)
    with pytest.raises(InvalidChannelContext) as error:
        service.ensure_identity(object())
    assert error.value.code == "INVALID_CONTEXT"
    assert counts(database) == (0, 0, 0, 0, 0)


def test_first_calls_converge_without_orphan_contacts(database):
    service = ChannelIdentityService(database)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: service.ensure_identity(context(event=str(uuid4()))), range(8)))
    assert len({item.identity_id for item in results}) == 1
    assert len({item.contact_id for item in results}) == 1
    assert sum(item.created for item in results) == 1
    assert counts(database)[:2] == (1, 1)


def test_persistence_failure_rolls_back_contact_and_hides_diagnostics(database, monkeypatch):
    service = ChannelIdentityService(database)
    def broken_insert(connection, channel_context):
        contact_id = uuid4()
        connection.execute(text("INSERT INTO contact_cards (id) VALUES (:id)"), {"id": contact_id})
        raise SQLAlchemyError("database secret 123")

    monkeypatch.setattr(service, "_insert_identity", broken_insert)
    with pytest.raises(ChannelIdentityPersistenceError) as error:
        service.ensure_identity(context())
    assert error.value.code == "IDENTITY_UNAVAILABLE"
    assert "123" not in str(error.value)
    assert counts(database) == (0, 0, 0, 0, 0)
