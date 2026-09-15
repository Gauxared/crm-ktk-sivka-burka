"""PostgreSQL implementation of the trusted ChannelGateway draft port.

This module receives an already-authenticated :class:`ChannelContext`; it does
not parse platform payloads or provision a contact/channel identity.  Event and
dialog identifiers are represented in event records only by keyed digests.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from .channel_gateway import ChannelContext, Draft, DraftSaved, GatewayError, ResetResult


IdentityResolver = Callable[[Any, ChannelContext], UUID | None]
_PATH = "/internal/channel/drafts"


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
        raise GatewayError("UNSUPPORTED_OPERATION")

    def submit_inquiry(self, context: ChannelContext, fields: Mapping[str, Any], draft_version: int, submit_event_id: str):
        raise GatewayError("UNSUPPORTED_OPERATION")

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
            prior = connection.execute(text("""SELECT payload_digest, result_json, tombstoned_at
                FROM operation_receipts
                WHERE scope_kind='CHANNEL_EVENT' AND scope_id=:scope_id AND method='EVENT'
                  AND canonical_path=:path AND key_digest=:event_digest"""), {
                "scope_id": scope_id, "path": _PATH, "event_digest": event_digest,
            }).mappings().one_or_none()
            if prior is not None:
                if not hmac.compare_digest(prior["payload_digest"], payload_digest):
                    raise GatewayError("IDEMPOTENCY_MISMATCH")
                if prior["tombstoned_at"] is not None or prior["result_json"] is None:
                    raise GatewayError("RESULT_EXPIRED")
                return dict(prior["result_json"])

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
