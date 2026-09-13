"""Transactional application command for accepting a public inquiry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import hmac
import json
import secrets
from typing import Any, Mapping
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, create_engine, text

from .db import database_url_from_environment


class InquiryCommandError(ValueError):
    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


class IdempotencyMismatch(InquiryCommandError):
    def __init__(self):
        super().__init__("IDEMPOTENCY_MISMATCH")


class SubmissionAlreadyUsed(InquiryCommandError):
    def __init__(self):
        super().__init__("SUBMISSION_ALREADY_USED")


class SubmissionExpired(InquiryCommandError):
    def __init__(self):
        super().__init__("SUBMISSION_EXPIRED")


class OptionUnavailable(InquiryCommandError):
    def __init__(self):
        super().__init__("OPTION_UNAVAILABLE")


@dataclass(frozen=True)
class InquiryAccepted:
    receipt_id: UUID
    inquiry_id: UUID
    replay: bool = False

    def public_result(self) -> dict[str, Any]:
        return {"receipt_id": str(self.receipt_id), "received": True, "booking_confirmed": False}


_ACQUISITION_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")
_MAX_TEXT = 2000


def _text(value: Any, field: str, *, max_length: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise InquiryCommandError("INVALID_REQUEST", f"invalid {field}")
    return value.strip()


def _optional_text(value: Any, field: str, *, max_length: int = _MAX_TEXT) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > max_length:
        raise InquiryCommandError("INVALID_REQUEST", f"invalid {field}")
    return value.strip()


def _canonical_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise InquiryCommandError("INVALID_REQUEST")
    requester = payload.get("requester")
    contact = requester.get("contact") if isinstance(requester, Mapping) else None
    requested = payload.get("requested_time") or {}
    if not isinstance(requested, Mapping):
        raise InquiryCommandError("INVALID_REQUEST", "invalid requested_time")
    name = _text(requester.get("name") if isinstance(requester, Mapping) else None, "requester.name", max_length=200)
    contact_kind = _text(contact.get("kind") if isinstance(contact, Mapping) else None, "requester.contact.kind", max_length=20).upper()
    if contact_kind not in {"PHONE", "TELEGRAM", "VK"}:
        raise InquiryCommandError("INVALID_REQUEST")
    contact_value = _text(contact.get("value") if isinstance(contact, Mapping) else None, "requester.contact.value", max_length=300)
    option_id = _text(payload.get("service_option_id"), "service_option_id", max_length=100)
    token = _text(payload.get("submission_token"), "submission_token", max_length=500)
    version = payload.get("catalog_version")
    participants = payload.get("participants_count")
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise InquiryCommandError("INVALID_REQUEST")
    if not isinstance(participants, int) or isinstance(participants, bool) or not 1 <= participants <= 100:
        raise InquiryCommandError("INVALID_REQUEST")
    requested_date = requested.get("date")
    try:
        parsed_date = date.fromisoformat(requested_date) if isinstance(requested_date, str) else None
    except ValueError as exc:
        raise InquiryCommandError("INVALID_REQUEST") from exc
    if parsed_date is None:
        raise InquiryCommandError("INVALID_REQUEST")
    time_text = _optional_text(requested.get("time_text"), "requested_time.time_text", max_length=200)
    experience = str(payload.get("experience", "UNKNOWN")).upper()
    if experience not in {"BEGINNER", "EXPERIENCED", "UNKNOWN"}:
        raise InquiryCommandError("INVALID_REQUEST")
    comment = _optional_text(payload.get("comment"), "comment") or ""
    acquisition = {
        key: value.strip()
        for key in _ACQUISITION_FIELDS
        if isinstance(value := (payload.get("acquisition") or {}).get(key), str)
        and len(value.strip()) <= 200
        and value.strip()
    }
    return {
        "submission_token": token,
        "catalog_version": version,
        "service_option_id": option_id,
        "requester": {"name": name, "contact": {"kind": contact_kind, "value": contact_value}},
        "participants_count": participants,
        "requested_time": {"date": parsed_date.isoformat(), "time_text": time_text},
        "experience": experience,
        "comment": comment,
        "acquisition": acquisition,
    }


def _digest(secret: bytes, value: str) -> bytes:
    return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).digest()


def _payload_digest(secret: bytes, payload: Mapping[str, Any]) -> bytes:
    semantic = dict(payload)
    semantic.pop("submission_token", None)
    encoded = json.dumps(semantic, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hmac.new(secret, encoded, hashlib.sha256).digest()


class PublicInquiryCommandService:
    def __init__(self, engine: Engine | None = None, *, hmac_secret: bytes | None = None, digest_key_version: int = 1):
        if not hmac_secret or not isinstance(hmac_secret, bytes):
            raise ValueError("hmac_secret must be injected runtime input")
        if digest_key_version <= 0:
            raise ValueError("digest_key_version must be positive")
        self.engine = engine or create_engine(database_url_from_environment())
        self.hmac_secret = hmac_secret
        self.digest_key_version = digest_key_version

    def submit(self, payload: Mapping[str, Any], idempotency_key: str) -> InquiryAccepted:
        normalized = _canonical_payload(payload)
        key_digest = _digest(self.hmac_secret, _text(idempotency_key, "Idempotency-Key", max_length=500))
        payload_digest = _payload_digest(self.hmac_secret, normalized)
        token_digest = _digest(self.hmac_secret, normalized["submission_token"])
        now = datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            connection.execute(text("SELECT id FROM business_write_guard WHERE id = 1 FOR UPDATE"))
            existing = connection.execute(
                text("""SELECT id, payload_digest, result_json, tombstoned_at
                       FROM operation_receipts
                       WHERE scope_kind = 'PUBLIC_FORM' AND scope_id = 'PUBLIC_FORM'
                         AND method = 'POST' AND canonical_path = '/api/v1/public/inquiries'
                         AND key_digest = :key_digest"""),
                {"key_digest": key_digest},
            ).mappings().first()
            if existing:
                if not hmac.compare_digest(existing["payload_digest"], payload_digest):
                    raise IdempotencyMismatch()
                if existing["tombstoned_at"] is not None or existing["result_json"] is None:
                    raise InquiryCommandError("RESULT_EXPIRED")
                result = existing["result_json"]
                return InquiryAccepted(UUID(str(existing["id"])), UUID(str(result["inquiry_id"])), replay=True)

            token = connection.execute(
                text("SELECT used_receipt_id, consumed_at, expires_at FROM submission_tokens WHERE token_digest = :digest FOR UPDATE"),
                {"digest": token_digest},
            ).mappings().first()
            if token is None:
                raise SubmissionExpired()
            if token["used_receipt_id"] is not None:
                if token["consumed_at"] and token["consumed_at"] <= now:
                    prior = connection.execute(text("SELECT payload_digest, result_json FROM operation_receipts WHERE id = :id"), {"id": token["used_receipt_id"]}).mappings().first()
                    if prior and hmac.compare_digest(prior["payload_digest"], payload_digest) and prior["result_json"]:
                        result = prior["result_json"]
                        return InquiryAccepted(UUID(str(token["used_receipt_id"])), UUID(str(result["inquiry_id"])), replay=True)
                raise SubmissionAlreadyUsed()
            if token["expires_at"] <= now:
                raise SubmissionExpired()

            option = connection.execute(
                text("""SELECT so.id, so.service_id, so.duration_minutes, so.pricing_mode, so.price_minor,
                              so.currency, so.code AS option_code, s.code AS service_code, s.title AS service_title,
                              s.description, s.information
                       FROM service_options so JOIN services s ON s.id = so.service_id
                       JOIN club_settings cs ON cs.id = 1
                       WHERE so.code = :code AND so.active AND s.active"""),
                {"code": normalized["service_option_id"]},
            ).mappings().first()
            if option is None:
                raise OptionUnavailable()
            settings = connection.execute(text("SELECT catalog_version, timezone FROM club_settings WHERE id = 1")).mappings().first()
            if settings is None:
                raise OptionUnavailable()

            inquiry_id, receipt_id, event_id, job_id = uuid4(), uuid4(), uuid4(), uuid4()
            contact_id = uuid4()
            received_at = now
            selection_snapshot = {
                "catalog_version": settings["catalog_version"], "service_option_id": str(option["id"]),
                "service_code": option["service_code"], "service_title": option["service_title"],
                "option_code": option["option_code"], "duration_minutes": option["duration_minutes"],
                "pricing_mode": option["pricing_mode"], "price_minor": option["price_minor"], "currency": option["currency"],
            }
            requester = normalized["requester"]
            connection.execute(text("INSERT INTO contact_cards (id) VALUES (:id)"), {"id": contact_id})
            connection.execute(text("""INSERT INTO inquiries
                (id, contact_id, source_kind, received_at, status, contact_snapshot, selection_snapshot,
                 requester_name, contact_kind, contact_value, requested_date, requested_time_text,
                 requested_timezone, experience, comment, acquisition)
                VALUES (:id, :contact_id, 'PUBLIC_FORM', :received_at, 'NEW', :contact_snapshot, :selection_snapshot,
                 :name, :contact_kind, :contact_value, :requested_date, :time_text, :timezone, :experience, :comment, :acquisition)"""), {
                "id": inquiry_id, "contact_id": contact_id, "received_at": received_at,
                "contact_snapshot": json.dumps(requester), "selection_snapshot": json.dumps(selection_snapshot),
                "name": requester["name"], "contact_kind": requester["contact"]["kind"], "contact_value": requester["contact"]["value"],
                "requested_date": date.fromisoformat(normalized["requested_time"]["date"]), "time_text": normalized["requested_time"]["time_text"],
                "timezone": settings["timezone"], "experience": normalized["experience"], "comment": normalized["comment"],
                "acquisition": json.dumps(normalized["acquisition"]),
            })
            total = option["price_minor"] * normalized["participants_count"] if option["pricing_mode"] == "FIXED_PER_PERSON" else None
            connection.execute(text("""INSERT INTO inquiry_terms
                (inquiry_id, service_option_id, participants_count, duration_minutes, total_minor, currency)
                VALUES (:inquiry_id, :option_id, :participants, :duration, :total, :currency)"""), {
                "inquiry_id": inquiry_id, "option_id": option["id"], "participants": normalized["participants_count"],
                "duration": option["duration_minutes"], "total": total, "currency": option["currency"],
            })
            connection.execute(text("""INSERT INTO operation_receipts
                (id, scope_kind, scope_id, method, canonical_path, key_digest, payload_digest,
                 digest_key_version, normalization_version, accepted_at, result_json)
                VALUES (:id, 'PUBLIC_FORM', 'PUBLIC_FORM', 'POST', '/api/v1/public/inquiries', :key, :payload, :key_version, 1, :accepted, :result)"""), {
                "id": receipt_id, "key": key_digest, "payload": payload_digest, "key_version": self.digest_key_version,
                "accepted": received_at, "result": json.dumps({"inquiry_id": str(inquiry_id)}),
            })
            connection.execute(text("UPDATE submission_tokens SET used_receipt_id = :receipt, consumed_at = :consumed WHERE token_digest = :token"), {"receipt": receipt_id, "consumed": received_at, "token": token_digest})
            connection.execute(text("""INSERT INTO change_events
                (id, command_id, ordinal, actor_kind, action, changes)
                VALUES (:id, :receipt, 1, 'SYSTEM', 'INQUIRY_RECEIVED', :changes)"""), {"id": event_id, "receipt": receipt_id, "changes": json.dumps({"after": {"status": "NEW", "inquiry_id": str(inquiry_id)}})})
            connection.execute(text("INSERT INTO event_inquiries (event_id, inquiry_id) VALUES (:event, :inquiry)"), {"event": event_id, "inquiry": inquiry_id})
            connection.execute(text("INSERT INTO notification_recipient_state (recipient_ref) VALUES ('OWNER_PRIMARY_UNCONFIGURED') ON CONFLICT DO NOTHING"))
            connection.execute(text("""INSERT INTO notification_jobs
                (id, origin_event_id, inquiry_id, recipient_ref, status, last_error_code)
                VALUES (:id, :event, :inquiry, 'OWNER_PRIMARY_UNCONFIGURED', 'BLOCKED', 'CONFIG_MISSING')"""), {"id": job_id, "event": event_id, "inquiry": inquiry_id})
        return InquiryAccepted(receipt_id, inquiry_id)


def generate_submission_token() -> str:
    """Generate an opaque token for a separate token-issuance command."""
    return secrets.token_urlsafe(32)
