"""Disposable PostgreSQL evidence for transactional channel submission."""
from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from apps.api.sivka_burka_api.channel_draft_persistence import PostgresChannelDraftPort
from apps.api.sivka_burka_api.channel_gateway import (
    ChannelContext,
    ChannelGateway,
    GatewayError,
    Platform,
)


ROOT = Path(__file__).resolve().parents[2]
SECRET = b"bot004-synthetic-secret"


def _synthetic_url():
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return make_url(line.removeprefix("DATABASE_URL="))
    raise RuntimeError("synthetic DATABASE_URL is absent from .env.example")


@pytest.fixture()
def database(monkeypatch):
    name = f"sivka_bot004_{uuid4().hex}"
    target = _synthetic_url().set(database=name)
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    engine = None
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
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


def context(*, platform=Platform.TELEGRAM, event="submit-1", sender="sender", conversation="conversation"):
    return ChannelContext(platform, "integration", sender, conversation, event)


def seed(database, contexts=(context(),)):
    service_id, option_id = uuid4(), uuid4()
    identities = {}
    with database.begin() as connection:
        connection.execute(text("""INSERT INTO club_settings
            (id, timezone, location_link, visit_rules)
            VALUES (1, 'Asia/Bangkok', 'https://example.invalid', 'Synthetic rules')"""))
        connection.execute(text("""INSERT INTO services
            (id, code, title, description, information, active, sort_order)
            VALUES (:id, 'ride', 'Ride', 'Synthetic', 'Synthetic', true, 1)"""), {"id": service_id})
        connection.execute(text("""INSERT INTO service_options
            (id, service_id, code, duration_minutes, pricing_mode, price_minor, currency, active)
            VALUES (:id, :service, 'ride-60', 60, 'FIXED_PER_PERSON', 350000, 'RUB', true)"""),
            {"id": option_id, "service": service_id})
        for item in contexts:
            contact_id, identity_id = uuid4(), uuid4()
            connection.execute(text("INSERT INTO contact_cards (id) VALUES (:id)"), {"id": contact_id})
            connection.execute(text("""INSERT INTO channel_identities
                (id, contact_id, platform, integration_id, external_sender_id)
                VALUES (:id, :contact, :platform, :integration, :sender)"""), {
                "id": identity_id,
                "contact": contact_id,
                "platform": item.platform.value,
                "integration": item.integration_id,
                "sender": item.external_sender_id,
            })
            identities[item.namespace()] = (identity_id, contact_id)
    return option_id, identities


def fields(option_id, *, contact=True, comment="Synthetic inquiry"):
    requester = {"name": "Synthetic Client"}
    if contact:
        requester["contact"] = {"kind": "PHONE", "value": "+70000000000"}
    return {
        "service_option_id": str(option_id),
        "requester": requester,
        "participants_count": 2,
        "requested_time": {"date": "2026-10-01", "time_text": "after 14:00"},
        "experience": "BEGINNER",
        "comment": comment,
    }


def gateway(database):
    return ChannelGateway(PostgresChannelDraftPort(database, SECRET))


def open_draft(service, submit_context):
    save = ChannelContext(
        submit_context.platform,
        submit_context.integration_id,
        submit_context.external_sender_id,
        submit_context.conversation_id,
        f"save-{submit_context.event_id}",
    )
    return service.save_draft(
        save,
        expected_version=0,
        answers={"step": "review"},
        step="REVIEW",
        event_id=save.event_id,
    )


def test_submit_commits_one_linked_inquiry_and_exact_replay_is_read_only(database):
    submit = context()
    option_id, identities = seed(database, (submit,))
    identity_id, contact_id = identities[submit.namespace()]
    service = gateway(database)
    assert open_draft(service, submit).version == 1

    first = service.submit_inquiry(
        submit, fields=fields(option_id), draft_version=1, submit_event_id=submit.event_id
    )
    replay = service.submit_inquiry(
        submit, fields=fields(option_id), draft_version=1, submit_event_id=submit.event_id
    )
    assert replay == type(first)(first.receipt_id, first.inquiry_id, replay=True)

    with database.connect() as connection:
        inquiry = connection.execute(text("""SELECT contact_id, channel_identity_id, source_kind,
            status, requester_name, contact_kind, contact_value, requested_date,
            requested_time_text, requested_timezone, experience, comment, contact_snapshot,
            selection_snapshot FROM inquiries""")).mappings().one()
        terms = connection.execute(text("SELECT * FROM inquiry_terms")).mappings().one()
        draft = connection.execute(text("SELECT version, closed_at FROM conversation_drafts")).one()
        assert inquiry["contact_id"] == contact_id
        assert inquiry["channel_identity_id"] == identity_id
        assert inquiry["source_kind"] == "TELEGRAM" and inquiry["status"] == "NEW"
        assert inquiry["requester_name"] == "Synthetic Client"
        assert (inquiry["contact_kind"], inquiry["contact_value"]) == ("PHONE", "+70000000000")
        assert str(inquiry["requested_date"]) == "2026-10-01"
        assert inquiry["requested_time_text"] == "after 14:00"
        assert inquiry["requested_timezone"] == "Asia/Bangkok"
        assert (inquiry["experience"], inquiry["comment"]) == ("BEGINNER", "Synthetic inquiry")
        assert inquiry["contact_snapshot"]["contact"]["kind"] == "PHONE"
        assert inquiry["selection_snapshot"]["service_option_id"] == str(option_id)
        assert terms["inquiry_id"] == UUID(first.inquiry_id)
        assert (terms["participants_count"], terms["duration_minutes"], terms["total_minor"]) == (2, 60, 700000)
        assert draft[0] == 2 and draft[1] is not None
        counts = {
            table: connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in (
                "contact_cards", "inquiries", "inquiry_terms", "receipt_inquiries",
                "change_events", "event_inquiries", "notification_jobs",
            )
        }
        assert counts == {
            "contact_cards": 1,
            "inquiries": 1,
            "inquiry_terms": 1,
            "receipt_inquiries": 1,
            "change_events": 1,
            "event_inquiries": 1,
            "notification_jobs": 1,
        }
        assert connection.execute(text("SELECT status FROM notification_jobs")).scalar_one() == "BLOCKED"
        assert connection.execute(text("SELECT count(*) FROM operation_receipts")).scalar_one() == 2
        assert connection.execute(text("SELECT array_agg(outcome_kind ORDER BY outcome_kind) FROM channel_events")).scalar_one() == ["SAVE", "SUBMIT"]


def test_trusted_channel_contact_fallback_and_telegram_vk_namespaces(database):
    telegram = context(event="shared-event", sender="tg-sender", conversation="tg-dialog")
    vk = context(platform=Platform.VK, event="shared-event", sender="vk-sender", conversation="vk-dialog")
    option_id, identities = seed(database, (telegram, vk))
    service = gateway(database)
    for item in (telegram, vk):
        open_draft(service, item)
        receipt = service.submit_inquiry(
            item, fields=fields(option_id, contact=False), draft_version=1,
            submit_event_id=item.event_id,
        )
        assert receipt.replay is False

    with database.connect() as connection:
        rows = connection.execute(text("""SELECT source_kind, contact_kind, contact_value,
            contact_id, channel_identity_id FROM inquiries ORDER BY source_kind""")).mappings().all()
        assert [(r["source_kind"], r["contact_kind"], r["contact_value"]) for r in rows] == [
            ("TELEGRAM", "TELEGRAM", "tg-sender"),
            ("VK", "VK", "vk-sender"),
        ]
        for row in rows:
            expected = identities[
                (row["source_kind"], "integration", row["contact_value"],
                 "tg-dialog" if row["source_kind"] == "TELEGRAM" else "vk-dialog")
            ]
            assert (row["channel_identity_id"], row["contact_id"]) == expected
        assert connection.execute(text("SELECT count(*) FROM inquiries")).scalar_one() == 2


def test_event_mismatch_stale_inactive_and_strict_rejections_are_atomic(database):
    submit = context(event="submit-atomic")
    option_id, _ = seed(database, (submit,))
    service = gateway(database)
    open_draft(service, submit)
    baseline = {"inquiries": 0, "receipts": 1, "events": 1, "version": 1}

    attempts = [
        lambda: service.submit_inquiry(
            submit, fields={**fields(option_id), "source_kind": "VK"},
            draft_version=1, submit_event_id=submit.event_id,
        ),
        lambda: service.submit_inquiry(
            submit, fields=fields(option_id), draft_version=2,
            submit_event_id=submit.event_id,
        ),
    ]
    for attempt in attempts:
        with pytest.raises(GatewayError):
            attempt()

    with database.begin() as connection:
        connection.execute(text("UPDATE service_options SET active=false WHERE id=:id"), {"id": option_id})
    with pytest.raises(GatewayError) as inactive:
        service.submit_inquiry(
            submit, fields=fields(option_id), draft_version=1,
            submit_event_id=submit.event_id,
        )
    assert inactive.value.code == "OPTION_UNAVAILABLE"
    with database.connect() as connection:
        observed = {
            "inquiries": connection.execute(text("SELECT count(*) FROM inquiries")).scalar_one(),
            "receipts": connection.execute(text("SELECT count(*) FROM operation_receipts")).scalar_one(),
            "events": connection.execute(text("SELECT count(*) FROM channel_events")).scalar_one(),
            "version": connection.execute(text("SELECT version FROM conversation_drafts")).scalar_one(),
        }
    assert observed == baseline


def test_cross_operation_reuse_changed_submit_expiry_and_tombstone_are_typed(database):
    submit = context(event="submit-final")
    option_id, _ = seed(database, (submit,))
    service = gateway(database)
    open_draft(service, submit)

    save_event = ChannelContext(Platform.TELEGRAM, "integration", "sender", "conversation", "save-submit-final")
    with pytest.raises(GatewayError) as cross_operation:
        service.submit_inquiry(
            save_event, fields=fields(option_id), draft_version=1,
            submit_event_id=save_event.event_id,
        )
    assert cross_operation.value.code == "IDEMPOTENCY_MISMATCH"

    reset = context(event="reset-final")
    service.reset_draft(reset, expected_version=1, event_id=reset.event_id)
    with pytest.raises(GatewayError) as reset_reuse:
        service.submit_inquiry(
            reset, fields=fields(option_id), draft_version=1,
            submit_event_id=reset.event_id,
        )
    assert reset_reuse.value.code == "IDEMPOTENCY_MISMATCH"
    reopen = context(event="reopen-final")
    service.save_draft(
        reopen, expected_version=0, answers={"step": "review"},
        step="REVIEW", event_id=reopen.event_id,
    )

    accepted = service.submit_inquiry(
        submit, fields=fields(option_id), draft_version=1, submit_event_id=submit.event_id
    )
    with pytest.raises(GatewayError) as changed:
        service.submit_inquiry(
            submit, fields=fields(option_id, comment="changed"), draft_version=1,
            submit_event_id=submit.event_id,
        )
    assert changed.value.code == "IDEMPOTENCY_MISMATCH"
    with pytest.raises(GatewayError) as changed_version:
        service.submit_inquiry(
            submit, fields=fields(option_id), draft_version=2,
            submit_event_id=submit.event_id,
        )
    assert changed_version.value.code == "IDEMPOTENCY_MISMATCH"

    with database.begin() as connection:
        connection.execute(text("""UPDATE operation_receipts
            SET result_json=NULL, result_expires_at=NULL, tombstoned_at=now()
            WHERE id=:id"""), {"id": UUID(accepted.receipt_id)})
    with pytest.raises(GatewayError) as expired_result:
        service.submit_inquiry(
            submit, fields=fields(option_id), draft_version=1,
            submit_event_id=submit.event_id,
        )
    assert expired_result.value.code == "RESULT_EXPIRED"

    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM inquiries")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM channel_events")).scalar_one() == 4


def test_expired_open_draft_cannot_be_submitted_or_partially_closed(database):
    submit = context(event="expired-submit")
    option_id, _ = seed(database, (submit,))
    service = gateway(database)
    open_draft(service, submit)
    with database.begin() as connection:
        connection.execute(text("UPDATE conversation_drafts SET expires_at=now() - interval '1 second'"))
    with pytest.raises(GatewayError) as expired:
        service.submit_inquiry(
            submit, fields=fields(option_id), draft_version=1,
            submit_event_id=submit.event_id,
        )
    assert expired.value.code == "VERSION_CONFLICT"
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM inquiries")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM operation_receipts")).scalar_one() == 1
        assert connection.execute(text("SELECT version FROM conversation_drafts")).scalar_one() == 1
