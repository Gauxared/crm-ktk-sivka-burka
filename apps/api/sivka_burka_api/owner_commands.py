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
class InquiryConfirmed:
    command_id: UUID
    inquiry_id: UUID
    inquiry_version: int
    visit_id: UUID
    visit_version: int
    replay: bool = False


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

    def confirm_inquiry(self, inquiry_id: UUID | str, payload: Mapping[str, Any], idempotency_key: str) -> InquiryConfirmed:
        target_inquiry_id = _uuid(str(inquiry_id))
        normalized = self._confirm_payload(target_inquiry_id, payload)
        path = f"/api/v1/admin/inquiries/{target_inquiry_id}/commands"
        key_digest, payload_digest = self._digests(idempotency_key, normalized)
        with self.engine.begin() as connection:
            connection.execute(text("SELECT id FROM business_write_guard WHERE id = 1 FOR UPDATE"))
            replay = self._replay(connection, path, key_digest, payload_digest)
            if replay is not None:
                return InquiryConfirmed(UUID(replay["command_id"]), UUID(replay["inquiry_id"]), int(replay["inquiry_version"]), UUID(replay["visit_id"]), int(replay["visit_version"]), True)
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
            result = {"command_id": str(command_id), "inquiry_id": str(target_inquiry_id), "inquiry_version": inquiry_version, "visit_id": str(visit_id), "visit_version": visit_version}
            connection.execute(text("INSERT INTO visit_participations (id, inquiry_id, visit_id, joined_at) VALUES (:id, :inquiry_id, :visit_id, :joined_at)"), {"id": uuid4(), "inquiry_id": target_inquiry_id, "visit_id": visit_id, "joined_at": now})
            connection.execute(text("UPDATE inquiries SET status = 'CONFIRMED', version = :version WHERE id = :id"), {"id": target_inquiry_id, "version": inquiry_version})
            connection.execute(text("UPDATE visits SET version = :version WHERE id = :id"), {"id": visit_id, "version": visit_version})
            self._store_receipt(connection, command_id, path, key_digest, payload_digest, result, now)
            self._event(connection, event_id, command_id, "INQUIRY_CONFIRMED", {"before": {"inquiry_id": str(target_inquiry_id), "status": inquiry["status"], "version": inquiry["version"], "visit_id": str(visit_id), "visit_version": visit["version"]}, "after": {"inquiry_id": str(target_inquiry_id), "status": "CONFIRMED", "version": inquiry_version, "visit_id": str(visit_id), "visit_version": visit_version}})
            connection.execute(text("INSERT INTO event_inquiries (event_id, inquiry_id) VALUES (:event_id, :inquiry_id)"), {"event_id": event_id, "inquiry_id": target_inquiry_id})
            connection.execute(text("INSERT INTO event_visits (event_id, visit_id) VALUES (:event_id, :visit_id)"), {"event_id": event_id, "visit_id": visit_id})
        return InquiryConfirmed(command_id, target_inquiry_id, inquiry_version, visit_id, visit_version)

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
        result = _json_value(row["result_json"])
        result["command_id"] = str(row["id"])
        return result

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
