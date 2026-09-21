"""Pure mapping from trusted Telegram updates to explicit interaction intents."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from apps.api.sivka_burka_api.channel_gateway import ChannelContext, GatewayError, Platform, validate_context
from .telegram_updates import AcceptedTelegramUpdate, InputKind


class TelegramInteractionError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Invalid Telegram interaction")


class TelegramIntentKind(str, Enum):
    OPEN = "OPEN"
    HELP = "HELP"
    RESET = "RESET"
    SUBMIT = "SUBMIT"
    SELECT_SERVICE = "SELECT_SERVICE"
    TEXT_INPUT = "TEXT_INPUT"


@dataclass(frozen=True, slots=True)
class TelegramIntent:
    context: ChannelContext
    kind: TelegramIntentKind
    payload: Mapping[str, Any]
    callback_query_id: str | None = None


_COMMAND = re.compile(r"^/(start|help|reset)(?:@[A-Za-z0-9_]{1,32})?$", re.IGNORECASE)
_FIXED_CALLBACK = re.compile(r"^v1:([ohru])$")
_SERVICE_CALLBACK = re.compile(r"^v1:s:([1-9][0-9]*):(0|[1-9][0-9]*)$")
_MAX_PROTOCOL_INTEGER = 2_147_483_647


def _error(code: str) -> TelegramInteractionError:
    return TelegramInteractionError(code)


def _valid(update: Any) -> AcceptedTelegramUpdate:
    if type(update) is not AcceptedTelegramUpdate:
        raise _error("INVALID_UPDATE")
    try:
        context = validate_context(update.context)
    except GatewayError:
        raise _error("INVALID_CONTEXT") from None
    if context.platform is not Platform.TELEGRAM:
        raise _error("INVALID_CONTEXT")
    if update.input_kind is InputKind.TEXT:
        if (
            not isinstance(update.text, str)
            or not update.text
            or update.text.strip() != update.text
            or len(update.text) > 4096
            or update.data is not None
            or update.callback_query_id is not None
        ):
            raise _error("MALFORMED_UPDATE")
    elif update.input_kind is InputKind.CALLBACK:
        if (
            update.text is not None
            or not isinstance(update.data, str)
            or not update.data
            or len(update.data.encode("utf-8")) > 64
            or not isinstance(update.callback_query_id, str)
            or not update.callback_query_id
            or update.callback_query_id.strip() != update.callback_query_id
            or len(update.callback_query_id) > 256
        ):
            raise _error("MALFORMED_UPDATE")
    else:
        raise _error("MALFORMED_UPDATE")
    return update


def _service_values(data: str) -> tuple[int, int] | None:
    match = _SERVICE_CALLBACK.fullmatch(data)
    if match is None:
        return None
    catalog, ordinal = (int(value) for value in match.groups())
    if catalog > _MAX_PROTOCOL_INTEGER or ordinal > _MAX_PROTOCOL_INTEGER:
        return None
    return catalog, ordinal


class TelegramInteractionMapper:
    def map(self, update: AcceptedTelegramUpdate) -> TelegramIntent:
        update = _valid(update)
        if update.input_kind is InputKind.TEXT:
            text = update.text
            assert text is not None
            match = _COMMAND.fullmatch(text)
            if match:
                kind = {"start": TelegramIntentKind.OPEN, "help": TelegramIntentKind.HELP, "reset": TelegramIntentKind.RESET}[match.group(1).lower()]
                return TelegramIntent(update.context, kind, MappingProxyType({}))
            if text.startswith("/"):
                raise _error("UNSUPPORTED_COMMAND")
            return TelegramIntent(update.context, TelegramIntentKind.TEXT_INPUT, MappingProxyType({"text": text}))

        data = update.data
        callback_id = update.callback_query_id
        assert data is not None and callback_id is not None
        fixed = _FIXED_CALLBACK.fullmatch(data)
        if fixed:
            kind = {"o": TelegramIntentKind.OPEN, "h": TelegramIntentKind.HELP, "r": TelegramIntentKind.RESET, "u": TelegramIntentKind.SUBMIT}[fixed.group(1)]
            return TelegramIntent(update.context, kind, MappingProxyType({}), callback_id)
        service = _service_values(data)
        if service is not None:
            catalog, ordinal = service
            return TelegramIntent(update.context, TelegramIntentKind.SELECT_SERVICE, MappingProxyType({"catalog_version": catalog, "option_ordinal": ordinal}), callback_id)
        tokens = data.split(":")
        if len(tokens) >= 2 and tokens[0] == "v1" and (tokens[1] == "" or tokens[1] in {"o", "h", "r", "u", "s"}):
            raise _error("MALFORMED_CALLBACK")
        raise _error("UNSUPPORTED_CALLBACK")


map_telegram_interaction = TelegramInteractionMapper().map
