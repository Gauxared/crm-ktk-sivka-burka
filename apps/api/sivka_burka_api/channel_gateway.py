"""Deterministic, side-effect-free boundary for trusted Telegram/VK adapters.

This module deliberately contains no platform SDK, transport, persistence, or raw
event model.  Adapters validate platform authenticity before constructing Context.
"""
from __future__ import annotations

from dataclasses import dataclass
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


_ID_FIELDS = frozenset({"integration_id", "external_sender_id", "conversation_id", "event_id"})
_BUSINESS_FIELDS = frozenset({"service_option_id", "requester", "participants_count", "requested_time", "experience", "comment"})


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
        clean = _mapping(fields, _BUSINESS_FIELDS, "UNKNOWN_FIELD")
        if not isinstance(clean.get("service_option_id"), str) or not clean["service_option_id"]:
            raise GatewayError("INVALID_FORM")
        if isinstance(clean.get("participants_count"), bool) or not isinstance(clean.get("participants_count"), int) or clean["participants_count"] < 1:
            raise GatewayError("INVALID_FORM")
        return self._port.submit_inquiry(context, dict(clean), draft_version, submit_event_id)

    def reset_draft(self, context: ChannelContext, *, expected_version: int, event_id: str) -> ResetResult:
        context = validate_context(context)
        _version(expected_version, allow_zero=True)
        _id(event_id, "event_id")
        if event_id != context.event_id:
            raise GatewayError("INVALID_EVENT")
        return self._port.reset_draft(context, expected_version, event_id)
