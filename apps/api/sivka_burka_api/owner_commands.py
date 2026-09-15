"""Transactional owner commands, deliberately independent from HTTP composition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
from typing import Any, Mapping
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from sivka_burka_api.admin_inquiries import INQUIRY_DETAIL_KEYS, inquiry_detail_in_transaction


class OwnerCommandError(ValueError):
    """A safe, stable domain failure for a future owner HTTP adapter."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class IdempotencyMismatch(OwnerCommandError):
    def __init__(self) -> None:
        super().__init__("IDEMPOTENCY_MISMATCH")


@dataclass(frozen=True)
class PlannedVisitCreated:
    command_id: UUID
    visit_id: UUID
    version: int
    status: str
    replay: bool = False


@dataclass(frozen=True)
class CashNoteRecorded:
    command_id: UUID
    inquiry: Mapping[str, Any]
    changed_visits: tuple[Mapping[str, Any], ...]
    replay: bool = False

    def response(self) -> dict[str, Any]:
        return {"command_id": str(self.command_id), "inquiry": self.inquiry,
                "changed_visits": [dict(visit) for visit in self.changed_visits]}


@dataclass(frozen=True)
class InquiryConfirmed:
    command_id: UUID
    inquiry: Mapping[str, Any]
    changed_visits: tuple[Mapping[str, Any], ...]
    replay: bool = False

    def response(self) -> dict[str, Any]:
        """The stable, JSON-ready owner response persisted in the receipt."""
        return {"command_id": str(self.command_id), "inquiry": self.inquiry,
                "changed_visits": [dict(visit) for visit in self.changed_visits]}

    @property
    def inquiry_id(self) -> UUID:
        return UUID(str(self.inquiry["id"]))

    @property
    def inquiry_version(self) -> int:
        return int(self.inquiry["version"])

    @property
    def visit_id(self) -> UUID:
        return UUID(str(self.changed_visits[0]["id"]))

    @property
    def visit_version(self) -> int:
        return int(self.changed_visits[0]["version"])


@dataclass(frozen=True)
class InquiryCompleted:
    command_id: UUID
    inquiry: Mapping[str, Any]
    changed_visits: tuple[Mapping[str, Any], ...]
    replay: bool = False

    def response(self) -> dict[str, Any]:
        return {"command_id": str(self.command_id), "inquiry": self.inquiry,
                "changed_visits": [dict(v) for v in self.changed_visits]}


@dataclass(frozen=True)
class NegotiationStarted:
    command_id: UUID
    inquiry: Mapping[str, Any]
    changed_visits: tuple[Mapping[str, Any], ...] = ()
    replay: bool = False

    def response(self) -> dict[str, Any]:
        return {"command_id": str(self.command_id), "inquiry": self.inquiry,
                "changed_visits": [dict(visit) for visit in self.changed_visits]}


def _required_text(value: Any, field: str, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise OwnerCommandError("VALIDATION_ERROR")
    return value.strip()


def _uuid(value: Any) -> UUID:
    try:
        return UUID(_required_text(value, "id", 128))
    except ValueError as exc:
        raise OwnerCommandError("NOT_FOUND") from exc


def _positive_version(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise OwnerCommandError("EXPECTED_VERSION_REQUIRED")
    return value


def _digest(secret: bytes, value: str) -> bytes:
    return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).digest()


def _payload_digest(secret: bytes, payload: Mapping[str, Any]) -> bytes:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hmac.new(secret, encoded, hashlib.sha256).digest()


def _json_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, str)):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise OwnerCommandError("RESULT_EXPIRED")


class OwnerCommandService:
    """Owner-scoped command service with injected infrastructure and credentials."""

    def __init__(self, engine: Engine, owner_id: UUID, *, hmac_secret: bytes, digest_key_version: int = 1):
        if not isinstance(engine, Engine):
            raise ValueError("engine must be injected")
        if not isinstance(owner_id, UUID):
            raise ValueError("owner_id must be a trusted UUID")
        if not isinstance(hmac_secret, bytes) or not hmac_secret:
            raise ValueError("hmac_secret must be an injected non-empty bytes value")
        if not isinstance(digest_key_version, int) or isinstance(digest_key_version, bool) or digest_key_version <= 0:
            raise ValueError("digest_key_version must be positive")
        self.engine = engine
        self.owner_id = owner_id
        self.hmac_secret = hmac_secret
        self.digest_key_version = digest_key_version

    def create_planned_visit(self, payload: Mapping[str, Any], idempotency_key: str) -> PlannedVisitCreated:
        normalized = self._planned_visit_payload(payload)
        path = "/api/v1/admin/visits"
        key_digest, payload_digest = self._digests(idempotency_key, normalized)
        with self.engine.begin() as connection:
            connection.execute(text("SELECT id FROM business_write_guard WHERE id = 1 FOR UPDATE"))
            replay = self._replay(connection, path, key_digest, payload_digest)
            if replay is not None:
                return PlannedVisitCreated(UUID(replay["command_id"]), UUID(replay["visit_id"]), int(replay["version"]), str(replay["status"]), True)
            service = connection.execute(text("SELECT id FROM services WHERE id = :id AND active FOR UPDATE"), {"id": UUID(normalized["service_id"])}).mappings().one_or_none()
            if service is None:
                raise OwnerCommandError("OPTION_UNAVAILABLE")
            command_id, visit_id, event_id = uuid4(), uuid4(), uuid4()
            now = datetime.now(timezone.utc)
            result = {"command_id": str(command_id), "visit_id": str(visit_id), "version": 1, "status": "PLANNED"}
            connection.execute(text("""INSERT INTO visits (id, service_id, status, version, start_at, duration_minutes, created_at)
                VALUES (:id, :service_id, 'PLANNED', 1, :start_at, :duration, :created_at)"""), {"id": visit_id, "service_id": service["id"], "start_at": normalized["start_at"], "duration": normalized["duration_minutes"], "created_at": now})
            self._store_receipt(connection, command_id, path, key_digest, payload_digest, result, now)
            self._event(connection, event_id, command_id, "VISIT_PLANNED", {"after": {"visit_id": str(visit_id), "status": "PLANNED", "version": 1}})
            connection.execute(text("INSERT INTO event_visits (event_id, visit_id) VALUES (:event_id, :visit_id)"), {"event_id": event_id, "visit_id": visit_id})
        return PlannedVisitCreated(command_id, visit_id, 1, "PLANNED")

    def record_cash_note(self, inquiry_id: UUID | str, payload: Mapping[str, Any], idempotency_key: str) -> CashNoteRecorded:
        """Append one owner-recorded cash fact and persist its complete CRM result."""
        target_inquiry_id = _uuid(str(inquiry_id))
        normalized = self._cash_note_payload(target_inquiry_id, payload)
        path = f"/api/v1/admin/inquiries/{target_inquiry_id}/cash-notes"
        key_digest, payload_digest = self._digests(idempotency_key, normalized)
        with self.engine.begin() as connection:
            connection.execute(text("SELECT id FROM business_write_guard WHERE id = 1 FOR UPDATE"))
            replay = self._replay(connection, path, key_digest, payload_digest)
            if replay is not None:
                return self._cash_note_result(replay, replay=True)
            inquiry = connection.execute(text("SELECT id, status, version FROM inquiries WHERE id=:id FOR UPDATE"), {"id": target_inquiry_id}).mappings().one_or_none()
            if inquiry is None:
                raise OwnerCommandError("NOT_FOUND")
            participation = connection.execute(text("""
                SELECT p.id, v.id AS visit_id, v.version AS visit_version
                FROM visit_participations p JOIN visits v ON v.id=p.visit_id
                WHERE p.inquiry_id=:id AND p.closed_at IS NULL FOR UPDATE OF p, v
            """), {"id": target_inquiry_id}).mappings().one_or_none()
            expected_visits = normalized["expected_visit_versions"]
            if int(inquiry["version"]) != normalized["expected_version"]:
                raise OwnerCommandError("VERSION_CONFLICT")
            if participation is None:
                if expected_visits:
                    raise OwnerCommandError("EXPECTED_VERSION_REQUIRED")
            else:
                visit_id = str(participation["visit_id"])
                if set(expected_visits) != {visit_id}:
                    raise OwnerCommandError("EXPECTED_VERSION_REQUIRED")
                if int(participation["visit_version"]) != expected_visits[visit_id]:
                    raise OwnerCommandError("VERSION_CONFLICT")
            command_id, event_id, note_id = uuid4(), uuid4(), uuid4()
            now = datetime.now(timezone.utc)
            inquiry_version = int(inquiry["version"]) + 1
            changed_visits: list[dict[str, Any]] = []
            connection.execute(text("""INSERT INTO cash_notes
                (id, inquiry_id, kind, amount_minor, currency, occurred_at, owner_id, note)
                VALUES (:id, :inquiry_id, :kind, :amount_minor, 'RUB', :occurred_at, :owner_id, :note)"""), {
                "id": note_id, "inquiry_id": target_inquiry_id, "kind": normalized["kind"],
                "amount_minor": normalized["amount_minor"], "occurred_at": normalized["occurred_at"],
                "owner_id": self.owner_id, "note": normalized["note"],
            })
            connection.execute(text("UPDATE inquiries SET version=:version WHERE id=:id"), {"id": target_inquiry_id, "version": inquiry_version})
            if participation is not None:
                visit_version = int(participation["visit_version"]) + 1
                connection.execute(text("UPDATE visits SET version=:version WHERE id=:id"), {"id": participation["visit_id"], "version": visit_version})
                changed_visits.append({"id": str(participation["visit_id"]), "version": visit_version})
            detail = inquiry_detail_in_transaction(connection, target_inquiry_id)
            if detail is None:
                raise OwnerCommandError("NOT_FOUND")
            result = {"command_id": str(command_id), "inquiry": detail, "changed_visits": changed_visits}
            self._store_receipt(connection, command_id, path, key_digest, payload_digest, result, now)
            connection.execute(text("INSERT INTO receipt_inquiries (receipt_id, inquiry_id) VALUES (:receipt_id, :inquiry_id)"), {"receipt_id": command_id, "inquiry_id": target_inquiry_id})
            self._event(connection, event_id, command_id, "CASH_NOTE_RECORDED", {
                "before": {"inquiry_id": str(target_inquiry_id), "version": inquiry["version"]},
                "after": {"inquiry_id": str(target_inquiry_id), "version": inquiry_version, "cash_note_id": str(note_id), "kind": normalized["kind"], "amount_minor": normalized["amount_minor"]},
            })
            connection.execute(text("INSERT INTO event_inquiries (event_id, inquiry_id) VALUES (:event_id, :inquiry_id)"), {"event_id": event_id, "inquiry_id": target_inquiry_id})
            if participation is not None:
                connection.execute(text("INSERT INTO event_visits (event_id, visit_id) VALUES (:event_id, :visit_id)"), {"event_id": event_id, "visit_id": participation["visit_id"]})
        return self._cash_note_result(result)

    def correct_cash_note(self, inquiry_id: UUID | str, note_id: UUID | str, payload: Mapping[str, Any], idempotency_key: str) -> CashNoteRecorded:
        """Correct one effective immutable manual cash note."""
        target_inquiry_id = _uuid(str(inquiry_id))
        target_note_id = _uuid(str(note_id))
        normalized = self._cash_correction_payload(target_inquiry_id, target_note_id, payload)
        path = f"/api/v1/admin/inquiries/{target_inquiry_id}/cash-notes/{target_note_id}/corrections"
        key_digest, payload_digest = self._digests(idempotency_key, normalized)
        with self.engine.begin() as connection:
            connection.execute(text("SELECT id FROM business_write_guard WHERE id = 1 FOR UPDATE"))
            replay = self._replay(connection, path, key_digest, payload_digest)
            if replay is not None:
                return self._cash_note_result(replay, replay=True)
            inquiry = connection.execute(text("SELECT id, status, version FROM inquiries WHERE id=:id FOR UPDATE"), {"id": target_inquiry_id}).mappings().one_or_none()
            if inquiry is None:
                raise OwnerCommandError("NOT_FOUND")
            participation = connection.execute(text("""
                SELECT p.id, v.id AS visit_id, v.version AS visit_version
                FROM visit_participations p JOIN visits v ON v.id=p.visit_id
                WHERE p.inquiry_id=:id AND p.closed_at IS NULL FOR UPDATE OF p, v
            """), {"id": target_inquiry_id}).mappings().one_or_none()
            expected_visits = normalized["expected_visit_versions"]
            if int(inquiry["version"]) != normalized["expected_version"]:
                raise OwnerCommandError("VERSION_CONFLICT")
            if participation is None:
                if expected_visits:
                    raise OwnerCommandError("EXPECTED_VERSION_REQUIRED")
            else:
                visit_id = str(participation["visit_id"])
                if set(expected_visits) != {visit_id}:
                    raise OwnerCommandError("EXPECTED_VERSION_REQUIRED")
                if int(participation["visit_version"]) != expected_visits[visit_id]:
                    raise OwnerCommandError("VERSION_CONFLICT")
            note = connection.execute(text("""
                SELECT id, inquiry_id FROM cash_notes
                WHERE id=:note_id AND inquiry_id=:inquiry_id FOR UPDATE
            """), {"note_id": target_note_id, "inquiry_id": target_inquiry_id}).mappings().one_or_none()
            if note is None:
                raise OwnerCommandError("NOT_FOUND")
            if connection.execute(text("SELECT 1 FROM cash_note_corrections WHERE original_note_id=:id"), {"id": target_note_id}).scalar_one_or_none() is not None:
                raise OwnerCommandError("CASH_NOTE_SUPERSEDED")
            command_id, event_id, correction_id = uuid4(), uuid4(), uuid4()
            replacement_id = None
            now = datetime.now(timezone.utc)
            if normalized["replacement"] is not None:
                replacement_id = uuid4()
                replacement = normalized["replacement"]
                connection.execute(text("""INSERT INTO cash_notes
                    (id, inquiry_id, kind, amount_minor, currency, occurred_at, owner_id, note)
                    VALUES (:id, :inquiry_id, :kind, :amount_minor, 'RUB', :occurred_at, :owner_id, :note)"""), {
                    "id": replacement_id, "inquiry_id": target_inquiry_id, "kind": replacement["kind"],
                    "amount_minor": replacement["amount_minor"], "occurred_at": replacement["occurred_at"],
                    "owner_id": self.owner_id, "note": replacement["note"],
                })
            connection.execute(text("""INSERT INTO cash_note_corrections
                (id, inquiry_id, original_note_id, replacement_note_id, reason, owner_id)
                VALUES (:id, :inquiry_id, :original, :replacement, :reason, :owner_id)"""), {
                "id": correction_id, "inquiry_id": target_inquiry_id, "original": target_note_id,
                "replacement": replacement_id, "reason": normalized["reason"], "owner_id": self.owner_id,
            })
            inquiry_version = int(inquiry["version"]) + 1
            connection.execute(text("UPDATE inquiries SET version=:version WHERE id=:id"), {"id": target_inquiry_id, "version": inquiry_version})
            changed_visits: list[dict[str, Any]] = []
            if participation is not None:
                visit_version = int(participation["visit_version"]) + 1
                connection.execute(text("UPDATE visits SET version=:version WHERE id=:id"), {"id": participation["visit_id"], "version": visit_version})
                changed_visits.append({"id": str(participation["visit_id"]), "version": visit_version})
            detail = inquiry_detail_in_transaction(connection, target_inquiry_id)
            if detail is None:
                raise OwnerCommandError("NOT_FOUND")
            result = {"command_id": str(command_id), "inquiry": detail, "changed_visits": changed_visits}
            self._store_receipt(connection, command_id, path, key_digest, payload_digest, result, now)
            connection.execute(text("INSERT INTO receipt_inquiries (receipt_id, inquiry_id) VALUES (:receipt_id, :inquiry_id)"), {"receipt_id": command_id, "inquiry_id": target_inquiry_id})
            self._event(connection, event_id, command_id, "CASH_NOTE_CORRECTED", {"before": {"inquiry_id": str(target_inquiry_id), "version": inquiry["version"], "cash_note_id": str(target_note_id)}, "after": {"inquiry_id": str(target_inquiry_id), "version": inquiry_version, "correction_id": str(correction_id), "replacement_note_id": str(replacement_id) if replacement_id else None}})
            connection.execute(text("INSERT INTO event_inquiries (event_id, inquiry_id) VALUES (:event_id, :inquiry_id)"), {"event_id": event_id, "inquiry_id": target_inquiry_id})
            if participation is not None:
                connection.execute(text("INSERT INTO event_visits (event_id, visit_id) VALUES (:event_id, :visit_id)"), {"event_id": event_id, "visit_id": participation["visit_id"]})
        return self._cash_note_result(result)

    def confirm_inquiry(self, inquiry_id: UUID | str, payload: Mapping[str, Any], idempotency_key: str) -> InquiryConfirmed:
        target_inquiry_id = _uuid(str(inquiry_id))
        normalized = self._confirm_payload(target_inquiry_id, payload)
        path = f"/api/v1/admin/inquiries/{target_inquiry_id}/commands"
        key_digest, payload_digest = self._digests(idempotency_key, normalized)
        with self.engine.begin() as connection:
            connection.execute(text("SELECT id FROM business_write_guard WHERE id = 1 FOR UPDATE"))
            replay = self._replay(connection, path, key_digest, payload_digest)
            if replay is not None:
                return self._confirmed_result(replay, replay=True)
            inquiry = connection.execute(text("""SELECT i.id, i.status, i.version,
                       so.service_id AS selected_service_id, t.duration_minutes AS agreed_duration_minutes
                       FROM inquiries i
                       JOIN inquiry_terms t ON t.inquiry_id = i.id
                       JOIN service_options so ON so.id = t.service_option_id
                       WHERE i.id = :id FOR UPDATE"""), {"id": target_inquiry_id}).mappings().one_or_none()
            if inquiry is None:
                raise OwnerCommandError("NOT_FOUND")
            visit_id = UUID(normalized["payload"]["visit_id"])
            visit = connection.execute(text("SELECT id, service_id, status, version, duration_minutes FROM visits WHERE id = :id FOR UPDATE"), {"id": visit_id}).mappings().one_or_none()
            if visit is None:
                raise OwnerCommandError("NOT_FOUND")
            if inquiry["version"] != normalized["expected_version"] or visit["version"] != normalized["expected_visit_versions"][str(visit_id)]:
                raise OwnerCommandError("VERSION_CONFLICT")
            if inquiry["selected_service_id"] != visit["service_id"] or (
                inquiry["agreed_duration_minutes"] is not None
                and visit["duration_minutes"] is not None
                and inquiry["agreed_duration_minutes"] != visit["duration_minutes"]
            ):
                raise OwnerCommandError("PLAN_CONFLICT")
            existing = connection.execute(text("SELECT id FROM visit_participations WHERE inquiry_id = :id AND closed_at IS NULL FOR UPDATE"), {"id": target_inquiry_id}).scalar_one_or_none()
            if existing is not None:
                raise OwnerCommandError("PARTICIPATION_CONFLICT")
            if inquiry["status"] not in {"NEW", "NEGOTIATING"} or visit["status"] != "PLANNED":
                raise OwnerCommandError("INVALID_TRANSITION")
            command_id, event_id = uuid4(), uuid4()
            now = datetime.now(timezone.utc)
            inquiry_version, visit_version = int(inquiry["version"]) + 1, int(visit["version"]) + 1
            connection.execute(text("INSERT INTO visit_participations (id, inquiry_id, visit_id, joined_at) VALUES (:id, :inquiry_id, :visit_id, :joined_at)"), {"id": uuid4(), "inquiry_id": target_inquiry_id, "visit_id": visit_id, "joined_at": now})
            connection.execute(text("UPDATE inquiries SET status = 'CONFIRMED', version = :version WHERE id = :id"), {"id": target_inquiry_id, "version": inquiry_version})
            connection.execute(text("UPDATE visits SET version = :version WHERE id = :id"), {"id": visit_id, "version": visit_version})
            detail = inquiry_detail_in_transaction(connection, target_inquiry_id)
            if detail is None:
                raise OwnerCommandError("NOT_FOUND")
            result = {"command_id": str(command_id), "inquiry": detail, "changed_visits": [{"id": str(visit_id), "version": visit_version}]}
            self._store_receipt(connection, command_id, path, key_digest, payload_digest, result, now)
            self._event(connection, event_id, command_id, "INQUIRY_CONFIRMED", {"before": {"inquiry_id": str(target_inquiry_id), "status": inquiry["status"], "version": inquiry["version"], "visit_id": str(visit_id), "visit_version": visit["version"]}, "after": {"inquiry_id": str(target_inquiry_id), "status": "CONFIRMED", "version": inquiry_version, "visit_id": str(visit_id), "visit_version": visit_version}})
            connection.execute(text("INSERT INTO event_inquiries (event_id, inquiry_id) VALUES (:event_id, :inquiry_id)"), {"event_id": event_id, "inquiry_id": target_inquiry_id})
            connection.execute(text("INSERT INTO event_visits (event_id, visit_id) VALUES (:event_id, :visit_id)"), {"event_id": event_id, "visit_id": visit_id})
        return self._confirmed_result(result)

    def complete_inquiry(self, inquiry_id: UUID | str, payload: Mapping[str, Any], idempotency_key: str) -> InquiryCompleted:
        target = _uuid(str(inquiry_id))
        normalized = self._complete_payload(target, payload)
        path = f"/api/v1/admin/inquiries/{target}/commands"
        key_digest, payload_digest = self._digests(idempotency_key, normalized)
        with self.engine.begin() as connection:
            connection.execute(text("SELECT id FROM business_write_guard WHERE id = 1 FOR UPDATE"))
            replay = self._replay(connection, path, key_digest, payload_digest)
            if replay is not None:
                return self._completed_result(replay, replay=True)
            inquiry = connection.execute(text("SELECT id, status, version FROM inquiries WHERE id=:id FOR UPDATE"), {"id": target}).mappings().one_or_none()
            if inquiry is None:
                raise OwnerCommandError("NOT_FOUND")
            if inquiry["status"] != "CONFIRMED":
                raise OwnerCommandError("INVALID_TRANSITION")
            parts = connection.execute(text("""SELECT p.id, p.visit_id, v.version AS visit_version,
                    v.service_id, v.status AS visit_status, v.start_at, v.duration_minutes
                    FROM visit_participations p JOIN visits v ON v.id=p.visit_id
                    WHERE p.inquiry_id=:id AND p.closed_at IS NULL FOR UPDATE OF p, v"""), {"id": target}).mappings().all()
            if len(parts) != 1:
                raise OwnerCommandError("PARTICIPATION_CONFLICT")
            part = parts[0]
            terms = connection.execute(text("""SELECT service_option_id, participants_count, duration_minutes,
                    total_minor, expected_prepayment_minor, currency, note
                    FROM inquiry_terms WHERE inquiry_id=:id FOR UPDATE"""), {"id": target}).mappings().one_or_none()
            if terms is None:
                raise OwnerCommandError("NOT_FOUND")
            visit_key = str(part["visit_id"])
            expected = normalized["expected_visit_versions"]
            if set(expected) != {visit_key}:
                raise OwnerCommandError("EXPECTED_VERSION_REQUIRED")
            if int(inquiry["version"]) != normalized["expected_version"] or int(part["visit_version"]) != expected[visit_key]:
                raise OwnerCommandError("VERSION_CONFLICT")
            if part["visit_status"] != "PLANNED":
                raise OwnerCommandError("INVALID_TRANSITION")
            now = datetime.now(timezone.utc)
            command_id, event_id = uuid4(), uuid4()
            inquiry_version = int(inquiry["version"]) + 1
            visit_version = int(part["visit_version"]) + 1
            def iso(value: Any) -> str:
                return value.astimezone(timezone.utc).isoformat() if isinstance(value, datetime) else str(value)
            participants_snapshot = {"participants_count": int(terms["participants_count"])}
            plan_snapshot = {"visit": {"id": visit_key, "service_id": str(part["service_id"]),
                "start_at": iso(part["start_at"]), "duration_minutes": part["duration_minutes"]},
                "terms": {"service_option_id": str(terms["service_option_id"]), "participants_count": int(terms["participants_count"]),
                    "duration_minutes": terms["duration_minutes"], "total_minor": terms["total_minor"],
                    "expected_prepayment_minor": terms["expected_prepayment_minor"], "currency": terms["currency"], "note": terms["note"]}}
            connection.execute(text("""UPDATE visit_participations SET closed_at=:closed_at, close_kind='PROVIDED',
                participants_snapshot=CAST(:participants AS jsonb), plan_snapshot=CAST(:plan AS jsonb)
                WHERE id=:id"""), {"closed_at": now, "participants": json.dumps(participants_snapshot, separators=(",", ":")), "plan": json.dumps(plan_snapshot, separators=(",", ":")), "id": part["id"]})
            connection.execute(text("UPDATE inquiries SET status='COMPLETED', version=:version WHERE id=:id"), {"id": target, "version": inquiry_version})
            connection.execute(text("UPDATE visits SET version=:version WHERE id=:id"), {"id": part["visit_id"], "version": visit_version})
            detail = inquiry_detail_in_transaction(connection, target)
            if detail is None:
                raise OwnerCommandError("NOT_FOUND")
            result = {"command_id": str(command_id), "inquiry": detail, "changed_visits": [{"id": visit_key, "version": visit_version}]}
            self._store_receipt(connection, command_id, path, key_digest, payload_digest, result, now)
            connection.execute(text("INSERT INTO receipt_inquiries (receipt_id, inquiry_id) VALUES (:receipt_id, :inquiry_id)"), {"receipt_id": command_id, "inquiry_id": target})
            self._event(connection, event_id, command_id, "INQUIRY_COMPLETED", {"before": {"inquiry_id": str(target), "status": inquiry["status"], "version": inquiry["version"], "visit_id": visit_key, "visit_version": part["visit_version"]}, "after": {"inquiry_id": str(target), "status": "COMPLETED", "version": inquiry_version, "visit_id": visit_key, "visit_version": visit_version}})
            connection.execute(text("INSERT INTO event_inquiries (event_id, inquiry_id) VALUES (:event_id, :inquiry_id)"), {"event_id": event_id, "inquiry_id": target})
            connection.execute(text("INSERT INTO event_visits (event_id, visit_id) VALUES (:event_id, :visit_id)"), {"event_id": event_id, "visit_id": part["visit_id"]})
        return self._completed_result(result)

    def start_negotiation(self, inquiry_id: UUID | str, payload: Mapping[str, Any], idempotency_key: str) -> NegotiationStarted:
        target_inquiry_id = _uuid(str(inquiry_id))
        normalized = self._start_negotiation_payload(target_inquiry_id, payload)
        path = f"/api/v1/admin/inquiries/{target_inquiry_id}/commands"
        key_digest, payload_digest = self._digests(idempotency_key, normalized)
        with self.engine.begin() as connection:
            connection.execute(text("SELECT id FROM business_write_guard WHERE id = 1 FOR UPDATE"))
            replay = self._replay(connection, path, key_digest, payload_digest)
            if replay is not None:
                return self._negotiation_result(replay, replay=True)
            inquiry = connection.execute(text("SELECT id, status, version FROM inquiries WHERE id=:id FOR UPDATE"), {"id": target_inquiry_id}).mappings().one_or_none()
            if inquiry is None:
                raise OwnerCommandError("NOT_FOUND")
            if inquiry["status"] != "NEW":
                raise OwnerCommandError("INVALID_TRANSITION")
            if int(inquiry["version"]) != normalized["expected_version"]:
                raise OwnerCommandError("VERSION_CONFLICT")
            command_id, event_id = uuid4(), uuid4()
            now = datetime.now(timezone.utc)
            inquiry_version = int(inquiry["version"]) + 1
            connection.execute(text("UPDATE inquiries SET status='NEGOTIATING', version=:version WHERE id=:id"), {"id": target_inquiry_id, "version": inquiry_version})
            detail = inquiry_detail_in_transaction(connection, target_inquiry_id)
            if detail is None:
                raise OwnerCommandError("NOT_FOUND")
            result = {"command_id": str(command_id), "inquiry": detail, "changed_visits": []}
            self._store_receipt(connection, command_id, path, key_digest, payload_digest, result, now)
            connection.execute(text("INSERT INTO receipt_inquiries (receipt_id, inquiry_id) VALUES (:receipt_id, :inquiry_id)"), {"receipt_id": command_id, "inquiry_id": target_inquiry_id})
            self._event(connection, event_id, command_id, "INQUIRY_NEGOTIATION_STARTED", {"before": {"inquiry_id": str(target_inquiry_id), "status": inquiry["status"], "version": inquiry["version"]}, "after": {"inquiry_id": str(target_inquiry_id), "status": "NEGOTIATING", "version": inquiry_version}})
            connection.execute(text("INSERT INTO event_inquiries (event_id, inquiry_id) VALUES (:event_id, :inquiry_id)"), {"event_id": event_id, "inquiry_id": target_inquiry_id})
        return self._negotiation_result(result)

    def _digests(self, idempotency_key: str, payload: Mapping[str, Any]) -> tuple[bytes, bytes]:
        return _digest(self.hmac_secret, _required_text(idempotency_key, "Idempotency-Key")), _payload_digest(self.hmac_secret, payload)

    def _replay(self, connection: Any, path: str, key_digest: bytes, payload_digest: bytes) -> dict[str, Any] | None:
        row = connection.execute(text("""SELECT id, payload_digest, result_json, tombstoned_at FROM operation_receipts
            WHERE scope_kind = 'OWNER' AND scope_id = :owner_id AND method = 'POST'
              AND canonical_path = :path AND key_digest = :key_digest"""), {"owner_id": str(self.owner_id), "path": path, "key_digest": key_digest}).mappings().one_or_none()
        if row is None:
            return None
        if not hmac.compare_digest(row["payload_digest"], payload_digest):
            raise IdempotencyMismatch()
        if row["tombstoned_at"] is not None or row["result_json"] is None:
            raise OwnerCommandError("RESULT_EXPIRED")
        return _json_value(row["result_json"])

    @staticmethod
    def _cash_note_result(result: Mapping[str, Any], *, replay: bool = False) -> CashNoteRecorded:
        try:
            if set(result) != {"command_id", "inquiry", "changed_visits"}:
                raise ValueError
            command_id = UUID(str(result["command_id"]))
            inquiry = result["inquiry"]
            changed_visits = result["changed_visits"]
            if not isinstance(inquiry, Mapping) or set(inquiry) != INQUIRY_DETAIL_KEYS:
                raise ValueError
            UUID(str(inquiry["id"])); inquiry_version = int(inquiry["version"])
            if inquiry_version <= 0 or not isinstance(changed_visits, list) or len(changed_visits) > 1:
                raise ValueError
            if changed_visits and (not isinstance(changed_visits[0], Mapping) or set(changed_visits[0]) != {"id", "version"}):
                raise ValueError
            if changed_visits:
                UUID(str(changed_visits[0]["id"]))
                if int(changed_visits[0]["version"]) <= 0:
                    raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise OwnerCommandError("RESULT_EXPIRED") from exc
        inquiry_copy = json.loads(json.dumps(inquiry, separators=(",", ":")))
        return CashNoteRecorded(command_id, inquiry_copy, tuple(dict(visit) for visit in changed_visits), replay)

    @staticmethod
    def _confirmed_result(result: Mapping[str, Any], *, replay: bool = False) -> InquiryConfirmed:
        try:
            if set(result) != {"command_id", "inquiry", "changed_visits"}:
                raise ValueError
            command_id = UUID(str(result["command_id"]))
            inquiry = result["inquiry"]
            changed_visits = result["changed_visits"]
            if not isinstance(inquiry, Mapping) or set(inquiry) != INQUIRY_DETAIL_KEYS:
                raise ValueError
            if not isinstance(changed_visits, list) or len(changed_visits) != 1:
                raise ValueError
            if not isinstance(changed_visits[0], Mapping) or set(changed_visits[0]) != {"id", "version"}:
                raise ValueError
            UUID(str(inquiry["id"])); int(inquiry["version"])
            UUID(str(changed_visits[0]["id"])); version = int(changed_visits[0]["version"])
            if version <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise OwnerCommandError("RESULT_EXPIRED") from exc
        inquiry_copy = json.loads(json.dumps(inquiry, separators=(",", ":")))
        return InquiryConfirmed(command_id, inquiry_copy, (dict(changed_visits[0]),), replay)

    @staticmethod
    def _completed_result(result: Mapping[str, Any], *, replay: bool = False) -> InquiryCompleted:
        try:
            if set(result) != {"command_id", "inquiry", "changed_visits"} or not isinstance(result["inquiry"], Mapping) or set(result["inquiry"]) != INQUIRY_DETAIL_KEYS:
                raise ValueError
            command_id = UUID(str(result["command_id"]))
            visits = result["changed_visits"]
            if not isinstance(visits, list) or len(visits) != 1 or not isinstance(visits[0], Mapping) or set(visits[0]) != {"id", "version"}:
                raise ValueError
            UUID(str(result["inquiry"]["id"])); UUID(str(visits[0]["id"]))
            if int(result["inquiry"]["version"]) <= 0 or int(visits[0]["version"]) <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise OwnerCommandError("RESULT_EXPIRED") from exc
        return InquiryCompleted(command_id, json.loads(json.dumps(result["inquiry"], separators=(",", ":"))), (dict(visits[0]),), replay)

    @staticmethod
    def _negotiation_result(result: Mapping[str, Any], *, replay: bool = False) -> NegotiationStarted:
        try:
            if set(result) != {"command_id", "inquiry", "changed_visits"} or not isinstance(result["inquiry"], Mapping) or set(result["inquiry"]) != INQUIRY_DETAIL_KEYS:
                raise ValueError
            command_id = UUID(str(result["command_id"]))
            if not isinstance(result["changed_visits"], list) or result["changed_visits"]:
                raise ValueError
            int(result["inquiry"]["version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OwnerCommandError("RESULT_EXPIRED") from exc
        inquiry_copy = json.loads(json.dumps(result["inquiry"], separators=(",", ":")))
        return NegotiationStarted(command_id, inquiry_copy, (), replay)

    def _store_receipt(self, connection: Any, command_id: UUID, path: str, key_digest: bytes, payload_digest: bytes, result: Mapping[str, Any], now: datetime) -> None:
        connection.execute(text("""INSERT INTO operation_receipts
            (id, scope_kind, scope_id, method, canonical_path, key_digest, payload_digest, digest_key_version, normalization_version, accepted_at, result_json)
            VALUES (:id, 'OWNER', :owner_id, 'POST', :path, :key_digest, :payload_digest, :key_version, 1, :accepted_at, CAST(:result AS jsonb))"""), {"id": command_id, "owner_id": str(self.owner_id), "path": path, "key_digest": key_digest, "payload_digest": payload_digest, "key_version": self.digest_key_version, "accepted_at": now, "result": json.dumps(dict(result), separators=(",", ":"))})

    def _event(self, connection: Any, event_id: UUID, command_id: UUID, action: str, changes: Mapping[str, Any]) -> None:
        connection.execute(text("""INSERT INTO change_events (id, command_id, ordinal, actor_kind, action, changes)
            VALUES (:id, :command_id, 1, 'OWNER', :action, CAST(:changes AS jsonb))"""), {"id": event_id, "command_id": command_id, "action": action, "changes": json.dumps(dict(changes), separators=(",", ":"))})

    @staticmethod
    def _planned_visit_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping) or set(payload) != {"service_id", "start_at", "duration_minutes"}:
            raise OwnerCommandError("VALIDATION_ERROR")
        service_id = _uuid(payload["service_id"])
        start = payload["start_at"]
        if not isinstance(start, str):
            raise OwnerCommandError("VALIDATION_ERROR")
        try:
            start_at = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OwnerCommandError("VALIDATION_ERROR") from exc
        if start_at.tzinfo is None or start_at.utcoffset() != timezone.utc.utcoffset(start_at):
            raise OwnerCommandError("VALIDATION_ERROR")
        duration = payload["duration_minutes"]
        if duration is not None and (not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0):
            raise OwnerCommandError("VALIDATION_ERROR")
        return {"service_id": str(service_id), "start_at": start_at.astimezone(timezone.utc).isoformat(), "duration_minutes": duration}

    @staticmethod
    def _cash_note_payload(inquiry_id: UUID, payload: Mapping[str, Any]) -> dict[str, Any]:
        required = {"expected_version", "kind", "amount_minor", "occurred_at", "note"}
        if not isinstance(payload, Mapping) or not required.issubset(payload):
            raise OwnerCommandError("EXPECTED_VERSION_REQUIRED" if isinstance(payload, Mapping) and "expected_version" not in payload else "VALIDATION_ERROR")
        if set(payload) != required and set(payload) != required | {"expected_visit_versions"}:
            raise OwnerCommandError("VALIDATION_ERROR")
        expected_version = _positive_version(payload["expected_version"])
        raw_versions = payload.get("expected_visit_versions", {})
        if not isinstance(raw_versions, Mapping):
            raise OwnerCommandError("VALIDATION_ERROR")
        expected_visits: dict[str, int] = {}
        for raw_id, version in raw_versions.items():
            expected_visits[str(_uuid(raw_id))] = _positive_version(version)
        kind = payload["kind"]
        if kind not in {"RECEIPT", "REFUND_NOTE"}:
            raise OwnerCommandError("VALIDATION_ERROR")
        amount_minor = payload["amount_minor"]
        if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or not 1 <= amount_minor <= 9_000_000_000_000:
            raise OwnerCommandError("VALIDATION_ERROR")
        occurred = payload["occurred_at"]
        if not isinstance(occurred, str):
            raise OwnerCommandError("VALIDATION_ERROR")
        try:
            occurred_at = datetime.fromisoformat(occurred.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OwnerCommandError("VALIDATION_ERROR") from exc
        if occurred_at.tzinfo is None or occurred_at.utcoffset() != timezone.utc.utcoffset(occurred_at):
            raise OwnerCommandError("VALIDATION_ERROR")
        return {"inquiry_id": str(inquiry_id), "expected_version": expected_version,
                "expected_visit_versions": expected_visits, "kind": kind, "amount_minor": amount_minor,
                "occurred_at": occurred_at.astimezone(timezone.utc).isoformat(),
                "note": _required_text(payload["note"], "note", 2000)}

    @staticmethod
    def _cash_correction_payload(inquiry_id: UUID, note_id: UUID, payload: Mapping[str, Any]) -> dict[str, Any]:
        required = {"expected_version", "reason", "replacement"}
        if not isinstance(payload, Mapping) or not required.issubset(payload):
            raise OwnerCommandError("EXPECTED_VERSION_REQUIRED" if isinstance(payload, Mapping) and "expected_version" not in payload else "VALIDATION_ERROR")
        if set(payload) not in (required, required | {"expected_visit_versions"}):
            raise OwnerCommandError("VALIDATION_ERROR")
        raw_versions = payload.get("expected_visit_versions", {})
        if not isinstance(raw_versions, Mapping):
            raise OwnerCommandError("VALIDATION_ERROR")
        versions = {str(_uuid(k)): _positive_version(v) for k, v in raw_versions.items()}
        replacement = payload["replacement"]
        if replacement is not None:
            if not isinstance(replacement, Mapping) or set(replacement) != {"kind", "amount_minor", "occurred_at", "note"}:
                raise OwnerCommandError("VALIDATION_ERROR")
            kind = replacement["kind"]
            amount = replacement["amount_minor"]
            if kind not in {"RECEIPT", "REFUND_NOTE"} or not isinstance(amount, int) or isinstance(amount, bool) or not 1 <= amount <= 9_000_000_000_000:
                raise OwnerCommandError("VALIDATION_ERROR")
            occurred = replacement["occurred_at"]
            if not isinstance(occurred, str):
                raise OwnerCommandError("VALIDATION_ERROR")
            try:
                occurred_at = datetime.fromisoformat(occurred.replace("Z", "+00:00"))
            except ValueError as exc:
                raise OwnerCommandError("VALIDATION_ERROR") from exc
            if occurred_at.tzinfo is None or occurred_at.utcoffset() != timezone.utc.utcoffset(occurred_at):
                raise OwnerCommandError("VALIDATION_ERROR")
            replacement = {"kind": kind, "amount_minor": amount, "occurred_at": occurred_at.astimezone(timezone.utc).isoformat(), "note": _required_text(replacement["note"], "note", 2000)}
        return {"inquiry_id": str(inquiry_id), "note_id": str(note_id), "expected_version": _positive_version(payload["expected_version"]), "expected_visit_versions": versions, "reason": _required_text(payload["reason"], "reason", 500), "replacement": replacement}

    @staticmethod
    def _confirm_payload(inquiry_id: UUID, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise OwnerCommandError("VALIDATION_ERROR")
        required_versions = {"expected_version", "expected_visit_versions"}
        if not required_versions.issubset(payload):
            raise OwnerCommandError("EXPECTED_VERSION_REQUIRED")
        if set(payload) != required_versions | {"payload"}:
            raise OwnerCommandError("VALIDATION_ERROR")
        expected_version = _positive_version(payload["expected_version"])
        command_payload = payload["payload"]
        versions = payload["expected_visit_versions"]
        if not isinstance(command_payload, Mapping) or set(command_payload) != {"visit_id"} or not isinstance(versions, Mapping):
            raise OwnerCommandError("VALIDATION_ERROR")
        visit_id = _uuid(command_payload["visit_id"])
        normalized_versions: dict[str, int] = {}
        for raw_id, version in versions.items():
            normalized_versions[str(_uuid(raw_id))] = _positive_version(version)
        if set(normalized_versions) != {str(visit_id)}:
            raise OwnerCommandError("EXPECTED_VERSION_REQUIRED")
        return {"inquiry_id": str(inquiry_id), "expected_version": expected_version, "expected_visit_versions": normalized_versions, "payload": {"visit_id": str(visit_id)}}

    @staticmethod
    def _complete_payload(inquiry_id: UUID, payload: Mapping[str, Any]) -> dict[str, Any]:
        required = {"type", "expected_version", "expected_visit_versions", "payload"}
        if not isinstance(payload, Mapping) or set(payload) != required:
            raise OwnerCommandError("EXPECTED_VERSION_REQUIRED" if not isinstance(payload, Mapping) or "expected_version" not in payload else "VALIDATION_ERROR")
        if payload["type"] != "COMPLETE" or payload["payload"] != {}:
            raise OwnerCommandError("VALIDATION_ERROR")
        versions = payload["expected_visit_versions"]
        if not isinstance(versions, Mapping):
            raise OwnerCommandError("EXPECTED_VERSION_REQUIRED")
        normalized = {str(_uuid(k)): _positive_version(v) for k, v in versions.items()}
        return {"inquiry_id": str(inquiry_id), "type": "COMPLETE", "expected_version": _positive_version(payload["expected_version"]), "expected_visit_versions": normalized, "payload": {}}

    @staticmethod
    def _start_negotiation_payload(inquiry_id: UUID, payload: Mapping[str, Any]) -> dict[str, Any]:
        required = {"type", "expected_version", "expected_visit_versions", "payload"}
        if not isinstance(payload, Mapping) or set(payload) != required:
            raise OwnerCommandError("EXPECTED_VERSION_REQUIRED" if not isinstance(payload, Mapping) or "expected_version" not in payload else "VALIDATION_ERROR")
        if payload["type"] != "START_NEGOTIATION" or payload["payload"] != {}:
            raise OwnerCommandError("VALIDATION_ERROR")
        versions = payload["expected_visit_versions"]
        if not isinstance(versions, Mapping):
            raise OwnerCommandError("EXPECTED_VERSION_REQUIRED")
        if versions:
            raise OwnerCommandError("EXPECTED_VERSION_REQUIRED")
        return {"inquiry_id": str(inquiry_id), "type": "START_NEGOTIATION", "expected_version": _positive_version(payload["expected_version"]), "expected_visit_versions": {}}
