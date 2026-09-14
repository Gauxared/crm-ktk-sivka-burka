"""Isolated PostgreSQL tests for the owner command core."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL, make_url

from sivka_burka_api.owner_commands import OwnerCommandError, OwnerCommandService


ROOT = Path(__file__).resolve().parents[2]


def _synthetic_template_url() -> URL:
    """Use only the committed synthetic template, never a configured database."""
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return make_url(line.removeprefix("DATABASE_URL="))
    raise RuntimeError("synthetic PostgreSQL template is unavailable")


@pytest.fixture()
def database(monkeypatch: pytest.MonkeyPatch) -> Engine:
    source = _synthetic_template_url()
    target = source.set(database=f"sivka_cmd001_{uuid4().hex}")
    if not source.database or target.database == source.database:
        raise AssertionError("refusing to use the template database itself")
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
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
        cleanup = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
        try:
            with cleanup.connect() as connection:
                connection.execute(text(f'DROP DATABASE IF EXISTS "{target.database}" WITH (FORCE)'))
        finally:
            cleanup.dispose()


@pytest.fixture()
def seeded(database: Engine) -> tuple[OwnerCommandService, UUID, UUID]:
    owner_id, service_id = uuid4(), uuid4()
    with database.begin() as connection:
        connection.execute(text("INSERT INTO services (id, code, title, description, information, active, sort_order) VALUES (:id, 'ride', 'Ride', 'Synthetic', 'Synthetic', true, 1)"), {"id": service_id})
    return OwnerCommandService(database, owner_id, hmac_secret=b"synthetic-owner-secret", digest_key_version=3), owner_id, service_id


def _visit_payload(service_id: UUID, duration: int | None = 60) -> dict[str, object]:
    return {"service_id": str(service_id), "start_at": "2026-10-01T08:00:00Z", "duration_minutes": duration}


def _inquiry(database: Engine, status: str = "NEW", *, service_id: UUID | None = None, duration: int | None = 60) -> UUID:
    selected_service_id = service_id or uuid4()
    option_id, contact_id, inquiry_id = (uuid4() for _ in range(3))
    with database.begin() as connection:
        if service_id is None:
            connection.execute(text("INSERT INTO services (id, code, title, description, information, active, sort_order) VALUES (:id, :code, 'Inquiry ride', 'Synthetic', 'Synthetic', true, 2)"), {"id": selected_service_id, "code": f"inquiry-{selected_service_id.hex}"})
        connection.execute(text("INSERT INTO service_options (id, service_id, code, duration_minutes, pricing_mode, price_minor, currency, active) VALUES (:id, :service_id, :code, :duration, 'FIXED_PER_PERSON', 100, 'RUB', true)"), {"id": option_id, "service_id": selected_service_id, "code": f"option-{option_id.hex}", "duration": duration})
        connection.execute(text("INSERT INTO contact_cards (id) VALUES (:id)"), {"id": contact_id})
        connection.execute(text("""INSERT INTO inquiries (id, contact_id, source_kind, status, contact_snapshot, selection_snapshot, requester_name, contact_kind, contact_value)
            VALUES (:id, :contact_id, 'PHONE', :status, '{}'::jsonb, '{}'::jsonb, 'Synthetic', 'PHONE', '+70000000000')"""), {"id": inquiry_id, "contact_id": contact_id, "status": status})
        connection.execute(text("INSERT INTO inquiry_terms (inquiry_id, service_option_id, participants_count, duration_minutes, currency) VALUES (:inquiry_id, :option_id, 1, :duration, 'RUB')"), {"inquiry_id": inquiry_id, "option_id": option_id, "duration": duration})
    return inquiry_id


def _confirm(visit_id: UUID, inquiry_version: int = 1, visit_version: int = 1) -> dict[str, object]:
    return {"expected_version": inquiry_version, "expected_visit_versions": {str(visit_id): visit_version}, "payload": {"visit_id": str(visit_id)}}


def _response(result: object) -> dict[str, object]:
    return result.response()  # type: ignore[union-attr]


def test_create_planned_visit_is_idempotent_and_audited(seeded: tuple[OwnerCommandService, UUID, UUID], database: Engine):
    service, owner_id, service_id = seeded
    created = service.create_planned_visit(_visit_payload(service_id), "visit-key")
    replay = service.create_planned_visit(_visit_payload(service_id), "visit-key")
    assert replay == type(created)(created.command_id, created.visit_id, 1, "PLANNED", True)
    with database.connect() as connection:
        stored = connection.execute(text("SELECT status, version, duration_minutes FROM visits WHERE id=:id"), {"id": created.visit_id}).one()
        audit = connection.execute(text("""SELECT r.scope_kind, r.scope_id, r.canonical_path, e.actor_kind, e.action,
            (SELECT count(*) FROM event_visits WHERE event_id=e.id) FROM operation_receipts r JOIN change_events e ON e.command_id=r.id WHERE r.id=:id"""), {"id": created.command_id}).one()
    assert stored == ("PLANNED", 1, 60)
    assert audit == ("OWNER", str(owner_id), "/api/v1/admin/visits", "OWNER", "VISIT_PLANNED", 1)


@pytest.mark.parametrize("status", ["NEW", "NEGOTIATING"])
def test_confirm_accepts_eligible_inquiry_without_payment(seeded: tuple[OwnerCommandService, UUID, UUID], database: Engine, status: str):
    service, _, service_id = seeded
    inquiry_id = _inquiry(database, status, service_id=service_id)
    visit = service.create_planned_visit(_visit_payload(service_id), f"visit-{status}")
    result = service.confirm_inquiry(inquiry_id, _confirm(visit.visit_id), f"confirm-{status}")
    replay = service.confirm_inquiry(inquiry_id, _confirm(visit.visit_id), f"confirm-{status}")
    assert (result.inquiry_version, result.visit_version, result.replay) == (2, 2, False)
    assert replay.replay is True
    assert _response(replay) == _response(result)
    assert set(result.inquiry) == {
        "id", "version", "status", "requester_name", "service_title", "participants_count",
        "requested_time", "visit_id", "agreed_start_at", "received_at", "channel", "cash_summary",
        "contact_snapshot", "current_contact", "selected_option_snapshot", "agreed_terms", "comment",
        "owner_note", "acquisition", "participation", "cash_notes", "notification_summary", "allowed_commands",
    }
    assert result.inquiry["status"] == "CONFIRMED"
    assert result.inquiry["participation"]["visit_id"] == str(visit.visit_id)
    assert _response(result)["changed_visits"] == [{"id": str(visit.visit_id), "version": 2}]
    with database.connect() as connection:
        states = connection.execute(text("SELECT i.status, i.version, v.version, (SELECT count(*) FROM visit_participations p WHERE p.inquiry_id=i.id AND p.closed_at IS NULL) FROM inquiries i JOIN visits v ON v.id=:visit WHERE i.id=:inquiry"), {"inquiry": inquiry_id, "visit": visit.visit_id}).one()
        audit = connection.execute(text("""SELECT e.actor_kind, e.action, (SELECT count(*) FROM event_inquiries WHERE event_id=e.id),
            (SELECT count(*) FROM event_visits WHERE event_id=e.id) FROM change_events e WHERE e.command_id=:id"""), {"id": result.command_id}).one()
    assert states == ("CONFIRMED", 2, 2, 1)
    assert audit == ("OWNER", "INQUIRY_CONFIRMED", 1, 1)


def test_confirmation_receipt_is_complete_and_replay_is_stable_after_read_model_changes(seeded: tuple[OwnerCommandService, UUID, UUID], database: Engine):
    service, _, service_id = seeded
    inquiry_id = _inquiry(database, service_id=service_id)
    visit = service.create_planned_visit(_visit_payload(service_id), "stable-receipt-visit")
    first = service.confirm_inquiry(inquiry_id, _confirm(visit.visit_id), "stable-receipt")
    first_response = _response(first)
    with database.connect() as connection:
        stored = connection.execute(text("""SELECT result_json FROM operation_receipts
            WHERE id=:id"""), {"id": first.command_id}).scalar_one()
    assert json.loads(json.dumps(stored)) == first_response

    with database.begin() as connection:
        connection.execute(text("""UPDATE inquiries SET requester_name='Changed later', contact_value='+79999999999',
            contact_snapshot=CAST(:contact AS jsonb), selection_snapshot=CAST(:selection AS jsonb),
            owner_note='Changed later' WHERE id=:id"""), {
            "id": inquiry_id, "contact": json.dumps({"changed": True}), "selection": json.dumps({"changed": True}),
        })
    replay = service.confirm_inquiry(inquiry_id, _confirm(visit.visit_id), "stable-receipt")
    assert replay.replay is True
    assert json.dumps(_response(replay), separators=(",", ":"), sort_keys=True) == json.dumps(first_response, separators=(",", ":"), sort_keys=True)
    assert replay.inquiry["requester_name"] == "Synthetic"
    assert replay.inquiry["contact_snapshot"] == {}


@pytest.mark.parametrize("state", ["tombstoned", "incomplete"])
def test_confirmation_replay_with_expired_or_incomplete_result_is_not_reexecuted(seeded: tuple[OwnerCommandService, UUID, UUID], database: Engine, state: str):
    service, _, service_id = seeded
    inquiry_id = _inquiry(database, service_id=service_id)
    visit = service.create_planned_visit(_visit_payload(service_id), f"expired-{state}")
    accepted = service.confirm_inquiry(inquiry_id, _confirm(visit.visit_id), f"expired-{state}")
    with database.begin() as connection:
        if state == "tombstoned":
            connection.execute(text("""UPDATE operation_receipts SET result_json=NULL, result_expires_at=NULL,
                tombstoned_at=now() WHERE id=:id"""), {"id": accepted.command_id})
        else:
            connection.execute(text("UPDATE operation_receipts SET result_json=CAST(:result AS jsonb) WHERE id=:id"), {
                "id": accepted.command_id,
                "result": json.dumps({"command_id": str(accepted.command_id), "inquiry": {"id": str(inquiry_id), "version": 2}, "changed_visits": [{"id": str(visit.visit_id), "version": 2}]}),
            })
    with pytest.raises(OwnerCommandError, match="RESULT_EXPIRED"):
        service.confirm_inquiry(inquiry_id, _confirm(visit.visit_id), f"expired-{state}")
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM visit_participations WHERE inquiry_id=:id"), {"id": inquiry_id}).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM change_events WHERE command_id=:id"), {"id": accepted.command_id}).scalar_one() == 1


def test_confirmation_reused_key_with_changed_payload_is_rejected_without_extra_writes(seeded: tuple[OwnerCommandService, UUID, UUID], database: Engine):
    service, _, service_id = seeded
    inquiry_id = _inquiry(database, service_id=service_id)
    first = service.create_planned_visit(_visit_payload(service_id), "mismatch-first")
    second = service.create_planned_visit(_visit_payload(service_id), "mismatch-second")
    accepted = service.confirm_inquiry(inquiry_id, _confirm(first.visit_id), "confirmation-mismatch")
    with pytest.raises(OwnerCommandError, match="IDEMPOTENCY_MISMATCH"):
        service.confirm_inquiry(inquiry_id, _confirm(second.visit_id, inquiry_version=2), "confirmation-mismatch")
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM visit_participations WHERE inquiry_id=:id"), {"id": inquiry_id}).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM change_events WHERE command_id=:id"), {"id": accepted.command_id}).scalar_one() == 1


def test_confirmation_rejection_is_atomic_and_key_is_not_consumed(seeded: tuple[OwnerCommandService, UUID, UUID], database: Engine):
    service, _, service_id = seeded
    inquiry_id = _inquiry(database, service_id=service_id)
    visit = service.create_planned_visit(_visit_payload(service_id), "create")
    with pytest.raises(OwnerCommandError, match="VERSION_CONFLICT"):
        service.confirm_inquiry(inquiry_id, _confirm(visit.visit_id, visit_version=7), "bad-version")
    with database.connect() as connection:
        assert connection.execute(text("SELECT status, version FROM inquiries WHERE id=:id"), {"id": inquiry_id}).one() == ("NEW", 1)
        assert connection.execute(text("SELECT version FROM visits WHERE id=:id"), {"id": visit.visit_id}).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM operation_receipts WHERE canonical_path LIKE '/api/v1/admin/inquiries/%'")).scalar_one() == 0
    confirmed = service.confirm_inquiry(inquiry_id, _confirm(visit.visit_id), "bad-version")
    assert confirmed.inquiry_version == 2


def test_confirmation_requires_exact_target_version_and_never_replaces_participation(seeded: tuple[OwnerCommandService, UUID, UUID], database: Engine):
    service, _, service_id = seeded
    inquiry_id = _inquiry(database, service_id=service_id)
    first, second = service.create_planned_visit(_visit_payload(service_id), "one"), service.create_planned_visit(_visit_payload(service_id), "two")
    missing_version_payloads = [
        {"expected_visit_versions": {str(first.visit_id): 1}, "payload": {"visit_id": str(first.visit_id)}},
        {"expected_version": 1, "payload": {"visit_id": str(first.visit_id)}},
        {"expected_version": 1, "expected_visit_versions": {}, "payload": {"visit_id": str(first.visit_id)}},
    ]
    for missing_payload in missing_version_payloads:
        with pytest.raises(OwnerCommandError, match="EXPECTED_VERSION_REQUIRED"):
            service.confirm_inquiry(inquiry_id, missing_payload, "missing")
    with pytest.raises(OwnerCommandError, match="VALIDATION_ERROR"):
        service.confirm_inquiry(inquiry_id, {**_confirm(first.visit_id), "unexpected": True}, "malformed")
    service.confirm_inquiry(inquiry_id, _confirm(first.visit_id), "first")
    with pytest.raises(OwnerCommandError, match="PARTICIPATION_CONFLICT"):
        service.confirm_inquiry(inquiry_id, _confirm(second.visit_id, inquiry_version=2), "second")
    with database.connect() as connection:
        assert connection.execute(text("SELECT visit_id FROM visit_participations WHERE inquiry_id=:id AND closed_at IS NULL"), {"id": inquiry_id}).scalar_one() == first.visit_id


def test_invalid_service_and_reused_key_with_changed_payload_are_safe(seeded: tuple[OwnerCommandService, UUID, UUID], database: Engine):
    service, _, service_id = seeded
    with pytest.raises(OwnerCommandError, match="OPTION_UNAVAILABLE"):
        service.create_planned_visit(_visit_payload(uuid4()), "invalid")
    created = service.create_planned_visit(_visit_payload(service_id), "same")
    with pytest.raises(OwnerCommandError, match="IDEMPOTENCY_MISMATCH"):
        service.create_planned_visit(_visit_payload(service_id, 90), "same")
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM visits")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM operation_receipts")).scalar_one() == 1
    assert created.visit_id


def test_confirmation_rejects_incompatible_service_or_duration_without_mutation(seeded: tuple[OwnerCommandService, UUID, UUID], database: Engine):
    service, _, service_id = seeded
    visit = service.create_planned_visit(_visit_payload(service_id), "compatible-plan")
    incompatible_service = _inquiry(database)
    incompatible_duration = _inquiry(database, service_id=service_id, duration=30)
    for inquiry_id, key in ((incompatible_service, "wrong-service"), (incompatible_duration, "wrong-duration")):
        with pytest.raises(OwnerCommandError, match="PLAN_CONFLICT"):
            service.confirm_inquiry(inquiry_id, _confirm(visit.visit_id), key)
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM visit_participations")).scalar_one() == 0
        assert connection.execute(text("SELECT status, version FROM inquiries WHERE id=:id"), {"id": incompatible_service}).one() == ("NEW", 1)
        assert connection.execute(text("SELECT status, version FROM inquiries WHERE id=:id"), {"id": incompatible_duration}).one() == ("NEW", 1)
        assert connection.execute(text("SELECT version FROM visits WHERE id=:id"), {"id": visit.visit_id}).scalar_one() == 1


def test_confirmation_allows_an_explicitly_unknown_duration(seeded: tuple[OwnerCommandService, UUID, UUID], database: Engine):
    service, _, service_id = seeded
    inquiry_id = _inquiry(database, service_id=service_id, duration=None)
    visit = service.create_planned_visit(_visit_payload(service_id, 60), "known-plan")
    result = service.confirm_inquiry(inquiry_id, _confirm(visit.visit_id), "unknown-duration")
    assert (result.inquiry_version, result.visit_version) == (2, 2)


def test_inactive_service_cannot_create_a_plan(seeded: tuple[OwnerCommandService, UUID, UUID], database: Engine):
    service, _, service_id = seeded
    with database.begin() as connection:
        connection.execute(text("UPDATE services SET active=false WHERE id=:id"), {"id": service_id})
    with pytest.raises(OwnerCommandError, match="OPTION_UNAVAILABLE"):
        service.create_planned_visit(_visit_payload(service_id), "inactive")
    with database.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM visits")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM operation_receipts")).scalar_one() == 0
