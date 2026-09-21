"""Pure resolution of validated Telegram intents into channel actions."""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from apps.api.sivka_burka_api.channel_conversation import ChannelAction
from apps.api.sivka_burka_api.channel_gateway import (
    ChannelContext,
    GatewayError,
    Platform,
    validate_context,
)
from .telegram_interactions import TelegramIntent, TelegramIntentKind


class TelegramActionError(ValueError):
    """Safe resolver error whose representation never includes caller data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Invalid Telegram action")

    def __str__(self) -> str:
        return f"Telegram action error: {self.code}"

    def __repr__(self) -> str:
        return f"TelegramActionError({self.code!r})"


_MAX_INTEGER = 2_147_483_647
_DIRECT = {
    TelegramIntentKind.OPEN: "OPEN",
    TelegramIntentKind.HELP: "HELP",
    TelegramIntentKind.RESET: "RESET",
    TelegramIntentKind.SUBMIT: "SUBMIT",
}


def _fail(code: str) -> TelegramActionError:
    return TelegramActionError(code)


def _context(context: Any) -> ChannelContext:
    if type(context) is not ChannelContext:
        raise _fail("INVALID_CONTEXT")
    try:
        checked = validate_context(context)
    except GatewayError:
        raise _fail("INVALID_CONTEXT") from None
    if checked.platform is not Platform.TELEGRAM:
        raise _fail("INVALID_CONTEXT")
    return checked


def _intent(intent: Any) -> TelegramIntent:
    if type(intent) is not TelegramIntent:
        raise _fail("INVALID_INTENT")
    _context(intent.context)
    if type(intent.kind) is not TelegramIntentKind:
        raise _fail("INVALID_INTENT")
    if type(intent.payload) is not MappingProxyType:
        raise _fail("INVALID_INTENT")
    callback_id = intent.callback_query_id
    if callback_id is not None and (
        not isinstance(callback_id, str)
        or not callback_id
        or callback_id.strip() != callback_id
        or len(callback_id) > 256
    ):
        raise _fail("INVALID_INTENT")
    return intent


def _empty_payload(payload: Mapping[str, Any]) -> None:
    if set(payload) != set():
        raise _fail("INVALID_INTENT")


def _integer(value: Any, *, minimum: int = 1) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= _MAX_INTEGER


def _normalized_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 100
        and value == value.strip()
        and "\x00" not in value
        and unicodedata.normalize("NFC", value) == value
    )


def _catalog_version(catalog: Any) -> int:
    if not isinstance(catalog, Mapping):
        raise _fail("CATALOG_UNAVAILABLE")
    version = catalog.get("catalog_version")
    if not _integer(version):
        raise _fail("CATALOG_UNAVAILABLE")
    return version


def _text_payload(payload: Mapping[str, Any], callback_id: str | None) -> None:
    if set(payload) != {"text"} or callback_id is not None:
        raise _fail("INVALID_INTENT")
    text = payload.get("text")
    if (
        not isinstance(text, str)
        or not text
        or text.strip() != text
        or len(text) > 4096
    ):
        raise _fail("INVALID_INTENT")


def _select_payload(payload: Mapping[str, Any], callback_id: str | None) -> tuple[int, int]:
    if set(payload) != {"catalog_version", "option_ordinal"}:
        raise _fail("INVALID_INTENT")
    if callback_id is None:
        raise _fail("INVALID_INTENT")
    callback_version = payload.get("catalog_version")
    ordinal = payload.get("option_ordinal")
    if not _integer(callback_version) or not _integer(ordinal, minimum=0):
        raise _fail("INVALID_INTENT")
    return callback_version, ordinal


def _active_options(catalog: Any) -> tuple[int, tuple[str, ...]]:
    version = _catalog_version(catalog)
    services = catalog.get("services")
    if not isinstance(services, list):
        raise _fail("CATALOG_UNAVAILABLE")
    result: list[str] = []
    seen: set[str] = set()
    for service in services:
        if not isinstance(service, Mapping) or not isinstance(service.get("options"), list):
            raise _fail("CATALOG_UNAVAILABLE")
        for option in service["options"]:
            if not isinstance(option, Mapping):
                raise _fail("CATALOG_UNAVAILABLE")
            active = option.get("active", True)
            if active is not True and active is not False:
                raise _fail("CATALOG_UNAVAILABLE")
            if not active:
                continue
            option_id = option.get("id")
            if not _normalized_id(option_id):
                raise _fail("CATALOG_UNAVAILABLE")
            assert isinstance(option_id, str)
            if option_id in seen:
                raise _fail("CATALOG_UNAVAILABLE")
            seen.add(option_id)
            result.append(option_id)
    return version, tuple(result)


@dataclass(frozen=True, slots=True)
class TelegramActionResolver:
    """Resolve one validated intent against one caller-supplied live catalog."""

    def resolve(self, intent: TelegramIntent, catalog: Mapping[str, Any]) -> ChannelAction:
        intent = _intent(intent)
        kind = intent.kind
        if kind in _DIRECT:
            _empty_payload(intent.payload)
            return ChannelAction(_DIRECT[kind], MappingProxyType({}))
        if kind is TelegramIntentKind.TEXT_INPUT:
            _text_payload(intent.payload, intent.callback_query_id)
            raise _fail("SCREEN_REQUIRED")
        if kind is not TelegramIntentKind.SELECT_SERVICE:
            raise _fail("INVALID_INTENT")
        callback_version, ordinal = _select_payload(intent.payload, intent.callback_query_id)
        live_version, option_ids = _active_options(catalog)
        if live_version != callback_version:
            raise _fail("STALE_CATALOG")
        if ordinal >= len(option_ids):
            raise _fail("UNKNOWN_SERVICE_SELECTION")
        return ChannelAction(
            "SELECT_SERVICE",
            MappingProxyType({"service_option_id": option_ids[ordinal]}),
        )


resolve_telegram_action = TelegramActionResolver().resolve
