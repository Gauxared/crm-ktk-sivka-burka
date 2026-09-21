"""Deterministic, side-effect-free boundary for trusted Telegram/VK adapters.

This module deliberately contains no platform SDK, transport, persistence, or raw
event model.  Adapters validate platform authenticity before constructing Context.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence


class Platform(str, Enum):
    TELEGRAM = "TELEGRAM"
    VK = "VK"


class GatewayError(ValueError):
    """Safe typed contract error; message never contains user payload."""

    def __init__(self, code: str, message: str = "Invalid channel operation") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ChannelContext:
    platform: Platform
    integration_id: str
    external_sender_id: str
    conversation_id: str
    event_id: str

    def namespace(self) -> tuple[str, str, str, str]:
        return (self.platform.value, self.integration_id, self.external_sender_id, self.conversation_id)


@dataclass(frozen=True, slots=True)
class Draft:
    answers: Mapping[str, Any]
    step: str
    version: int
    expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogResult:
    catalog: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class DraftResult:
    draft: Draft | None


@dataclass(frozen=True, slots=True)
class DraftSaved:
    version: int
    step: str


@dataclass(frozen=True, slots=True)
class InquiryReceipt:
    receipt_id: str
    inquiry_id: str
    replay: bool = False


@dataclass(frozen=True, slots=True)
class ResetResult:
    reset: bool
    version: int


class ChannelPort(Protocol):
    def read_catalog(self, context: ChannelContext) -> Mapping[str, Any]: ...
    def read_draft(self, context: ChannelContext) -> Draft | None: ...
    def save_draft(self, context: ChannelContext, expected_version: int, answers: Mapping[str, Any], step: str, event_id: str) -> DraftSaved: ...
    def submit_inquiry(self, context: ChannelContext, fields: Mapping[str, Any], draft_version: int, submit_event_id: str) -> InquiryReceipt: ...
    def reset_draft(self, context: ChannelContext, expected_version: int, event_id: str) -> ResetResult: ...
    def read_event(self, context: ChannelContext) -> Mapping[str, Any] | None: ...


_ID_FIELDS = frozenset({"integration_id", "external_sender_id", "conversation_id", "event_id"})
_BUSINESS_FIELDS = frozenset({"service_option_id", "requester", "participants_count", "requested_time", "experience", "comment"})
_REQUESTER_FIELDS = frozenset({"name", "contact"})
_CONTACT_FIELDS = frozenset({"kind", "value"})
_REQUESTED_TIME_FIELDS = frozenset({"date", "time_text"})


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= 128 or value.strip() != value or "\x00" in value:
        raise GatewayError("INVALID_ID")
    return value


def _version(value: Any, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
        raise GatewayError("INVALID_VERSION")
    return value


def _mapping(value: Any, allowed: frozenset[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not set(value).issubset(allowed):
        raise GatewayError(code)
    return value


def _text(value: Any, code: str, max_length: int, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > max_length:
        raise GatewayError(code)
    normalized = value.strip()
    if not normalized and not optional:
        raise GatewayError(code)
    return normalized or None


def _submission_fields(context: ChannelContext, value: Mapping[str, Any]) -> dict[str, Any]:
    clean = _mapping(value, _BUSINESS_FIELDS, "UNKNOWN_FIELD")
    required = {"service_option_id", "requester", "participants_count", "requested_time"}
    if not required.issubset(clean):
        raise GatewayError("INVALID_FORM")

    requester = _mapping(clean["requester"], _REQUESTER_FIELDS, "INVALID_FORM")
    if "name" not in requester:
        raise GatewayError("INVALID_FORM")
    normalized_requester: dict[str, Any] = {
        "name": _text(requester["name"], "INVALID_FORM", 200)
    }
    if requester.get("contact") is not None:
        contact = _mapping(requester["contact"], _CONTACT_FIELDS, "INVALID_FORM")
        if set(contact) != _CONTACT_FIELDS:
            raise GatewayError("INVALID_FORM")
        kind = _text(contact["kind"], "INVALID_FORM", 20)
        assert kind is not None
        kind = kind.upper()
        if kind not in {"PHONE", "TELEGRAM", "VK"}:
            raise GatewayError("INVALID_FORM")
        normalized_requester["contact"] = {
            "kind": kind,
            "value": _text(contact["value"], "INVALID_FORM", 300),
        }

    requested = _mapping(clean["requested_time"], _REQUESTED_TIME_FIELDS, "INVALID_FORM")
    if "date" not in requested:
        raise GatewayError("INVALID_FORM")
    if not isinstance(requested["date"], str):
        raise GatewayError("INVALID_FORM")
    try:
        requested_date = date.fromisoformat(requested["date"]).isoformat()
    except (TypeError, ValueError):
        raise GatewayError("INVALID_FORM") from None
    time_text = _text(requested.get("time_text"), "INVALID_FORM", 200, optional=True)

    option_id = _text(clean["service_option_id"], "INVALID_FORM", 100)
    participants = clean["participants_count"]
    if isinstance(participants, bool) or not isinstance(participants, int) or not 1 <= participants <= 100:
        raise GatewayError("INVALID_FORM")
    experience = str(clean.get("experience", "UNKNOWN")).upper()
    if experience not in {"BEGINNER", "EXPERIENCED", "UNKNOWN"}:
        raise GatewayError("INVALID_FORM")
    comment = clean.get("comment", "")
    if not isinstance(comment, str) or len(comment) > 2000:
        raise GatewayError("INVALID_FORM")

    return {
        "service_option_id": option_id,
        "requester": normalized_requester,
        "participants_count": participants,
        "requested_time": {"date": requested_date, "time_text": time_text},
        "experience": experience,
        "comment": comment.strip(),
    }


def validate_context(context: ChannelContext) -> ChannelContext:
    if not isinstance(context, ChannelContext) or not isinstance(context.platform, Platform):
        raise GatewayError("INVALID_CONTEXT")
    for field in _ID_FIELDS:
        _id(getattr(context, field), field)
    return context


class ChannelGateway:
    """Validate the trusted boundary, then call the injected application port."""

    def __init__(self, port: ChannelPort) -> None:
        self._port = port

    def read_catalog(self, context: ChannelContext) -> CatalogResult:
        context = validate_context(context)
        return CatalogResult(self._port.read_catalog(context))

    def read_draft(self, context: ChannelContext) -> DraftResult:
        context = validate_context(context)
        return DraftResult(self._port.read_draft(context))

    def save_draft(self, context: ChannelContext, *, expected_version: int, answers: Mapping[str, Any], step: str, event_id: str) -> DraftSaved:
        context = validate_context(context)
        _version(expected_version, allow_zero=True)
        _id(event_id, "event_id")
        if event_id != context.event_id or not isinstance(step, str) or not 0 < len(step) <= 128:
            raise GatewayError("INVALID_EVENT")
        clean = _mapping(answers, frozenset(), "UNKNOWN_FIELD") if not isinstance(answers, Mapping) else answers
        if not isinstance(clean, Mapping):
            raise GatewayError("INVALID_ANSWERS")
        return self._port.save_draft(context, expected_version, dict(clean), step, event_id)

    def submit_inquiry(self, context: ChannelContext, *, fields: Mapping[str, Any], draft_version: int, submit_event_id: str) -> InquiryReceipt:
        context = validate_context(context)
        _version(draft_version)
        _id(submit_event_id, "submit_event_id")
        if submit_event_id != context.event_id:
            raise GatewayError("INVALID_EVENT")
        clean = _submission_fields(context, fields)
        return self._port.submit_inquiry(context, clean, draft_version, submit_event_id)

    def reset_draft(self, context: ChannelContext, *, expected_version: int, event_id: str) -> ResetResult:
        context = validate_context(context)
        _version(expected_version, allow_zero=True)
        _id(event_id, "event_id")
        if event_id != context.event_id:
            raise GatewayError("INVALID_EVENT")
        return self._port.reset_draft(context, expected_version, event_id)

    def read_event(self, context: ChannelContext) -> Mapping[str, Any] | None:
        """Read a privacy-preserving committed-event marker, if the port supports it."""
        context = validate_context(context)
        reader = getattr(self._port, "read_event", None)
        if not callable(reader):
            return None
        result = reader(context)
        if result is not None and (not isinstance(result, Mapping) or result.get("kind") not in {"SAVE", "RESET", "SUBMIT"}):
            raise GatewayError("INVALID_EVENT_RECEIPT")
        return result
