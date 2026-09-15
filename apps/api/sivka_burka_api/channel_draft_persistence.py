"""PostgreSQL implementation of the trusted ChannelGateway draft port.

This module receives an already-authenticated :class:`ChannelContext`; it does
not parse platform payloads or provision a contact/channel identity.  Event and
dialog identifiers are represented in event records only by keyed digests.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import json
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from .channel_gateway import (
    ChannelContext, Draft, DraftSaved, GatewayError, InquiryReceipt, ResetResult,
    _id, _submission_fields, _version, validate_context,
)
from .public_catalog import PublicCatalogRuntime, PublicCatalogUnavailable


IdentityResolver = Callable[[Any, ChannelContext], UUID | None]
_PATH = "/internal/channel/drafts"
_SUBMIT_PATH = "/internal/channel/inquiries"


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _digest(secret: bytes, value: bytes) -> bytes:
    return hmac.new(secret, value, hashlib.sha256).digest()


def _identity_from_context(connection: Any, context: ChannelContext) -> UUID | None:
    """Resolve only a pre-existing identity from the trusted adapter context."""
    value = connection.execute(
        text("""SELECT id FROM channel_identities
                WHERE platform=:platform AND integration_id=:integration_id
                  AND external_sender_id=:sender"""),
        {"platform": context.platform.value, "integration_id": context.integration_id,
         "sender": context.external_sender_id},
    ).scalar_one_or_none()
    return UUID(str(value)) if value is not None else None


@dataclass(frozen=True)
class PostgresChannelDraftPort:
    """Transactional ChannelPort for drafts, with stable event receipts."""

    engine: Engine
    hmac_secret: bytes
    draft_ttl: timedelta = timedelta(hours=24)
    digest_key_version: int = 1
    identity_resolver: IdentityResolver = _identity_from_context

    def __post_init__(self) -> None:
        if not isinstance(self.hmac_secret, bytes) or not self.hmac_secret:
            raise ValueError("hmac_secret must be supplied as non-empty runtime material")
        if not isinstance(self.draft_ttl, timedelta) or self.draft_ttl <= timedelta():
            raise ValueError("draft_ttl must be positive")
        if self.digest_key_version <= 0:
            raise ValueError("digest_key_version must be positive")

    def read_catalog(self, context: ChannelContext) -> Mapping[str, Any]:
        """Return the public projection without requiring channel state.

        The landing-page runtime is the sole owner of catalog projection and
        filtering semantics.  Keep this boundary read-only and deliberately
        hide database/configuration failures from trusted channel adapters.
        """
        validate_context(context)
        try:
            catalog = PublicCatalogRuntime(self.engine).read()
        except (PublicCatalogUnavailable, SQLAlchemyError):
            raise GatewayError("CATALOG_UNAVAILABLE") from None
        if not isinstance(catalog, Mapping) or not catalog.get("services"):
            raise GatewayError("CATALOG_UNAVAILABLE")
        return catalog

    def submit_inquiry(self, context: ChannelContext, fields: Mapping[str, Any], draft_version: int, submit_event_id: str) -> InquiryReceipt:
        context = validate_context(context)
        _version(draft_version)
        _id(submit_event_id, "submit_event_id")
        if submit_event_id != context.event_id:
            raise GatewayError("INVALID_EVENT")
        normalized = _submission_fields(context, fields)
        operation = {"kind": "SUBMIT", "draft_version": draft_version, "fields": normalized}
        now = datetime.now(timezone.utc)
        event_digest = _digest(self.hmac_secret, submit_event_id.encode("utf-8"))
        dialog_digest = _digest(
            self.hmac_secret,
            _canonical({"sender": context.external_sender_id, "conversation": context.conversation_id}),
        )
        payload_digest = _digest(self.hmac_secret, _canonical(operation))
        scope_id = f"{context.platform.value}:{context.integration_id}"

        with self.engine.begin() as connection:
            connection.execute(text("SELECT id FROM business_write_guard WHERE id=1 FOR UPDATE"))
            prior = self._event_replay(
                connection, context, event_digest, _SUBMIT_PATH, payload_digest
            )
            if prior is not None:
                return InquiryReceipt(
                    str(prior["receipt_id"]), str(prior["inquiry_id"]), replay=True
                )

            identity_id = self.identity_resolver(connection, context)
            if identity_id is None:
                raise GatewayError("CHANNEL_IDENTITY_NOT_FOUND")
            identity = connection.execute(
                text("SELECT contact_id FROM channel_identities WHERE id=:id"),
                {"id": identity_id},
            ).mappings().one_or_none()
            if identity is None:
                raise GatewayError("CHANNEL_IDENTITY_NOT_FOUND")
            draft = connection.execute(text("""SELECT id, version, expires_at
                FROM conversation_drafts
                WHERE channel_identity_id=:identity_id AND conversation_id=:conversation_id
                  AND closed_at IS NULL FOR UPDATE"""), {
                "identity_id": identity_id, "conversation_id": context.conversation_id,
            }).mappings().one_or_none()
            if (
                draft is None
                or draft["expires_at"] <= now
                or int(draft["version"]) != draft_version
            ):
                raise GatewayError("VERSION_CONFLICT")

            try:
                option_id = UUID(str(normalized["service_option_id"]))
            except ValueError:
                raise GatewayError("OPTION_UNAVAILABLE") from None
            option = connection.execute(text("""SELECT so.id, so.service_id, so.duration_minutes,
                       so.pricing_mode, so.price_minor, so.currency, so.code AS option_code,
                       s.code AS service_code, s.title AS service_title, s.description, s.information
                FROM service_options so JOIN services s ON s.id=so.service_id
                WHERE so.id=:option_id AND so.active AND s.active"""), {
                "option_id": option_id,
            }).mappings().one_or_none()
            settings = connection.execute(
                text("SELECT catalog_version, timezone FROM club_settings WHERE id=1")
            ).mappings().one_or_none()
            if option is None or settings is None:
                raise GatewayError("OPTION_UNAVAILABLE")

            requester = dict(normalized["requester"])
            contact = requester.get("contact") or {
                "kind": context.platform.value,
                "value": context.external_sender_id,
            }
            requester["contact"] = contact
            selection_snapshot = {
                "catalog_version": settings["catalog_version"],
                "service_option_id": str(option["id"]),
                "service_code": option["service_code"],
                "service_title": option["service_title"],
                "option_code": option["option_code"],
                "duration_minutes": option["duration_minutes"],
                "pricing_mode": option["pricing_mode"],
                "price_minor": option["price_minor"],
                "currency": option["currency"],
            }
            inquiry_id, receipt_id, audit_id, channel_event_id, job_id = (
                uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
            )
            connection.execute(text("""INSERT INTO inquiries
                (id, contact_id, channel_identity_id, source_kind, received_at, status,
                 contact_snapshot, selection_snapshot, requester_name, contact_kind, contact_value,
                 requested_date, requested_time_text, requested_timezone, experience, comment, acquisition)
                VALUES (:id, :contact_id, :identity_id, :source_kind, :received_at, 'NEW',
                 CAST(:contact_snapshot AS jsonb), CAST(:selection_snapshot AS jsonb), :name, :contact_kind,
                 :contact_value, :requested_date, :time_text, :timezone, :experience, :comment, '{}'::jsonb)"""), {
                "id": inquiry_id,
                "contact_id": identity["contact_id"],
                "identity_id": identity_id,
                "source_kind": context.platform.value,
                "received_at": now,
                "contact_snapshot": _canonical(requester).decode("utf-8"),
                "selection_snapshot": _canonical(selection_snapshot).decode("utf-8"),
                "name": requester["name"],
                "contact_kind": contact["kind"],
                "contact_value": contact["value"],
                "requested_date": date.fromisoformat(normalized["requested_time"]["date"]),
                "time_text": normalized["requested_time"]["time_text"],
                "timezone": settings["timezone"],
                "experience": normalized["experience"],
                "comment": normalized["comment"],
            })
            total = (
                option["price_minor"] * normalized["participants_count"]
                if option["pricing_mode"] == "FIXED_PER_PERSON" else None
            )
            connection.execute(text("""INSERT INTO inquiry_terms
                (inquiry_id, service_option_id, participants_count, duration_minutes,
                 total_minor, currency)
                VALUES (:inquiry_id, :option_id, :participants, :duration, :total, :currency)"""), {
                "inquiry_id": inquiry_id,
                "option_id": option["id"],
                "participants": normalized["participants_count"],
                "duration": option["duration_minutes"],
                "total": total,
                "currency": option["currency"],
            })
            connection.execute(
                text("UPDATE conversation_drafts SET version=:version, closed_at=:now WHERE id=:id"),
                {"version": draft_version + 1, "now": now, "id": draft["id"]},
            )
            result = {
                "kind": "SUBMIT",
                "receipt_id": str(receipt_id),
                "inquiry_id": str(inquiry_id),
            }
            connection.execute(text("""INSERT INTO operation_receipts
                (id, scope_kind, scope_id, method, canonical_path, key_digest, payload_digest,
                 digest_key_version, normalization_version, accepted_at, result_json)
                VALUES (:id, 'CHANNEL_EVENT', :scope_id, 'EVENT', :path, :key_digest, :payload_digest,
                        :digest_key_version, 1, :accepted_at, CAST(:result AS jsonb))"""), {
                "id": receipt_id,
                "scope_id": scope_id,
                "path": _SUBMIT_PATH,
                "key_digest": event_digest,
                "payload_digest": payload_digest,
                "digest_key_version": self.digest_key_version,
                "accepted_at": now,
                "result": _canonical(result).decode("utf-8"),
            })
            connection.execute(
                text("INSERT INTO receipt_inquiries (receipt_id, inquiry_id) VALUES (:receipt, :inquiry)"),
                {"receipt": receipt_id, "inquiry": inquiry_id},
            )
            connection.execute(text("""INSERT INTO change_events
                (id, command_id, ordinal, actor_kind, action, changes)
                VALUES (:id, :receipt, 1, 'CHANNEL', 'INQUIRY_RECEIVED', CAST(:changes AS jsonb))"""), {
                "id": audit_id,
                "receipt": receipt_id,
                "changes": _canonical({"after": {
                    "status": "NEW", "inquiry_id": str(inquiry_id),
                    "source_kind": context.platform.value,
                }}).decode("utf-8"),
            })
            connection.execute(
                text("INSERT INTO event_inquiries (event_id, inquiry_id) VALUES (:event, :inquiry)"),
                {"event": audit_id, "inquiry": inquiry_id},
            )
            connection.execute(text("""INSERT INTO channel_events
                (id, platform, integration_id, event_key_digest, dialog_key_digest,
                 digest_key_version, outcome_kind, receipt_id)
                VALUES (:id, :platform, :integration_id, :event_digest, :dialog_digest,
                        :digest_key_version, 'SUBMIT', :receipt_id)"""), {
                "id": channel_event_id,
                "platform": context.platform.value,
                "integration_id": context.integration_id,
                "event_digest": event_digest,
                "dialog_digest": dialog_digest,
                "digest_key_version": self.digest_key_version,
                "receipt_id": receipt_id,
            })
            connection.execute(text("""INSERT INTO notification_recipient_state (recipient_ref)
                VALUES ('OWNER_PRIMARY_UNCONFIGURED') ON CONFLICT DO NOTHING"""))
            connection.execute(text("""INSERT INTO notification_jobs
                (id, origin_event_id, inquiry_id, recipient_ref, status, last_error_code)
                VALUES (:id, :event, :inquiry, 'OWNER_PRIMARY_UNCONFIGURED', 'BLOCKED', 'CONFIG_MISSING')"""), {
                "id": job_id, "event": audit_id, "inquiry": inquiry_id,
            })
        return InquiryReceipt(str(receipt_id), str(inquiry_id), replay=False)

    def read_draft(self, context: ChannelContext) -> Draft | None:
        now = datetime.now(timezone.utc)
        with self.engine.connect() as connection:
            identity_id = self.identity_resolver(connection, context)
            if identity_id is None:
                return None
            row = connection.execute(text("""SELECT answers, step, version, expires_at
                FROM conversation_drafts
                WHERE channel_identity_id=:identity_id AND conversation_id=:conversation_id
                  AND closed_at IS NULL AND expires_at > :now"""), {
                "identity_id": identity_id, "conversation_id": context.conversation_id, "now": now,
            }).mappings().one_or_none()
        if row is None:
            return None
        return Draft(dict(row["answers"]), row["step"], int(row["version"]), row["expires_at"].isoformat())

    def save_draft(self, context: ChannelContext, expected_version: int, answers: Mapping[str, Any], step: str, event_id: str) -> DraftSaved:
        operation = {"kind": "SAVE", "expected_version": expected_version, "answers": dict(answers), "step": step}
        result = self._write(context, event_id, operation)
        return DraftSaved(int(result["version"]), str(result["step"]))

    def reset_draft(self, context: ChannelContext, expected_version: int, event_id: str) -> ResetResult:
        operation = {"kind": "RESET", "expected_version": expected_version}
        result = self._write(context, event_id, operation)
        return ResetResult(True, int(result["version"]))

    def _write(self, context: ChannelContext, event_id: str, operation: Mapping[str, Any]) -> Mapping[str, Any]:
        now = datetime.now(timezone.utc)
        event_digest = _digest(self.hmac_secret, event_id.encode("utf-8"))
        dialog_digest = _digest(self.hmac_secret, _canonical({"sender": context.external_sender_id, "conversation": context.conversation_id}))
        payload_digest = _digest(self.hmac_secret, _canonical(operation))
        scope_id = f"{context.platform.value}:{context.integration_id}"
        with self.engine.begin() as connection:
            connection.execute(text("SELECT id FROM business_write_guard WHERE id=1 FOR UPDATE"))
            prior = self._event_replay(connection, context, event_digest, _PATH, payload_digest)
            if prior is not None:
                return prior

            identity_id = self.identity_resolver(connection, context)
            if identity_id is None:
                raise GatewayError("CHANNEL_IDENTITY_NOT_FOUND")
            draft = connection.execute(text("""SELECT id, version, expires_at
                FROM conversation_drafts
                WHERE channel_identity_id=:identity_id AND conversation_id=:conversation_id
                  AND closed_at IS NULL FOR UPDATE"""), {
                "identity_id": identity_id, "conversation_id": context.conversation_id,
            }).mappings().one_or_none()
            if draft is not None and draft["expires_at"] <= now:
                connection.execute(text("UPDATE conversation_drafts SET closed_at=:now WHERE id=:id"), {"now": now, "id": draft["id"]})
                draft = None

            if operation["kind"] == "SAVE":
                if draft is None:
                    if operation["expected_version"] != 0:
                        raise GatewayError("VERSION_CONFLICT")
                    version = 1
                    connection.execute(text("""INSERT INTO conversation_drafts
                        (id, channel_identity_id, conversation_id, version, answers, step, expires_at)
                        VALUES (:id, :identity_id, :conversation_id, :version, CAST(:answers AS jsonb), :step, :expires_at)"""), {
                        "id": uuid4(), "identity_id": identity_id, "conversation_id": context.conversation_id,
                        "version": version, "answers": _canonical(operation["answers"]).decode("utf-8"),
                        "step": operation["step"], "expires_at": now + self.draft_ttl,
                    })
                else:
                    if int(draft["version"]) != operation["expected_version"]:
                        raise GatewayError("VERSION_CONFLICT")
                    version = int(draft["version"]) + 1
                    connection.execute(text("""UPDATE conversation_drafts
                        SET version=:version, answers=CAST(:answers AS jsonb), step=:step, expires_at=:expires_at
                        WHERE id=:id"""), {"version": version, "answers": _canonical(operation["answers"]).decode("utf-8"),
                                             "step": operation["step"], "expires_at": now + self.draft_ttl, "id": draft["id"]})
                result: dict[str, Any] = {"kind": "SAVE", "version": version, "step": operation["step"]}
            else:
                if draft is None or int(draft["version"]) != operation["expected_version"]:
                    raise GatewayError("VERSION_CONFLICT")
                version = int(draft["version"]) + 1
                connection.execute(text("UPDATE conversation_drafts SET version=:version, closed_at=:now WHERE id=:id"),
                                   {"version": version, "now": now, "id": draft["id"]})
                result = {"kind": "RESET", "version": version}

            receipt_id = uuid4()
            connection.execute(text("""INSERT INTO operation_receipts
                (id, scope_kind, scope_id, method, canonical_path, key_digest, payload_digest,
                 digest_key_version, normalization_version, accepted_at, result_json)
                VALUES (:id, 'CHANNEL_EVENT', :scope_id, 'EVENT', :path, :event_digest, :payload_digest,
                        :digest_key_version, 1, :now, CAST(:result AS jsonb))"""), {
                "id": receipt_id, "scope_id": scope_id, "path": _PATH, "event_digest": event_digest,
                "payload_digest": payload_digest, "digest_key_version": self.digest_key_version,
                "now": now, "result": _canonical(result).decode("utf-8"),
            })
            connection.execute(text("""INSERT INTO channel_events
                (id, platform, integration_id, event_key_digest, dialog_key_digest, digest_key_version,
                 outcome_kind, receipt_id)
                VALUES (:id, :platform, :integration_id, :event_digest, :dialog_digest, :digest_key_version,
                        :outcome_kind, :receipt_id)"""), {
                "id": uuid4(), "platform": context.platform.value, "integration_id": context.integration_id,
                "event_digest": event_digest, "dialog_digest": dialog_digest,
                "digest_key_version": self.digest_key_version, "outcome_kind": result["kind"], "receipt_id": receipt_id,
            })
            return result

    def _event_replay(
        self,
        connection: Any,
        context: ChannelContext,
        event_digest: bytes,
        path: str,
        payload_digest: bytes,
    ) -> dict[str, Any] | None:
        prior = connection.execute(text("""SELECT e.receipt_id, r.canonical_path,
                   r.payload_digest, r.result_json, r.tombstoned_at
            FROM channel_events e
            LEFT JOIN operation_receipts r ON r.id=e.receipt_id
            WHERE e.platform=:platform AND e.integration_id=:integration_id
              AND e.event_key_digest=:event_digest"""), {
            "platform": context.platform.value,
            "integration_id": context.integration_id,
            "event_digest": event_digest,
        }).mappings().one_or_none()
        if prior is None:
            return None
        if (
            prior["receipt_id"] is None
            or prior["canonical_path"] != path
            or not hmac.compare_digest(prior["payload_digest"], payload_digest)
        ):
            raise GatewayError("IDEMPOTENCY_MISMATCH")
        if prior["tombstoned_at"] is not None or prior["result_json"] is None:
            raise GatewayError("RESULT_EXPIRED")
        return dict(prior["result_json"])
