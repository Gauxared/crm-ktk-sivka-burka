"""Pure mapping from trusted Telegram updates to explicit interaction intents."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from apps.api.sivka_burka_api.channel_gateway import ChannelContext, Platform, validate_context
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
_CALLBACK = re.compile(r"^v1:([o h r u s])$".replace(" ", ""))
_SERVICE = re.compile(r"^v1:s:([0-9]+):([0-9]+)$")


def _error(code: str) -> TelegramInteractionError:
    return TelegramInteractionError(code)


def _valid(update: Any) -> AcceptedTelegramUpdate:
    if type(update) is not AcceptedTelegramUpdate:
        raise _error("INVALID_UPDATE")
    try:
        context = validate_context(update.context)
    except Exception:
        raise _error("INVALID_CONTEXT") from None
    if context.platform is not Platform.TELEGRAM:
        raise _error("INVALID_CONTEXT")
    if update.input_kind is InputKind.TEXT:
        if not isinstance(update.text, str) or update.data is not None:
            raise _error("MALFORMED_UPDATE")
    elif update.input_kind is InputKind.CALLBACK:
        if not isinstance(update.data, str) or update.text is not None or not isinstance(update.callback_query_id, str):
            raise _error("MALFORMED_UPDATE")
    else:
        raise _error("MALFORMED_UPDATE")
    return update


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
        fixed = _CALLBACK.fullmatch(data)
        if fixed:
            kind = {"o": TelegramIntentKind.OPEN, "h": TelegramIntentKind.HELP, "r": TelegramIntentKind.RESET, "u": TelegramIntentKind.SUBMIT}[fixed.group(1)]
            return TelegramIntent(update.context, kind, MappingProxyType({}), callback_id)
        service = _SERVICE.fullmatch(data)
        if service:
            catalog, ordinal = (int(value) for value in service.groups())
            if not 1 <= catalog <= 2147483647 or not 0 <= ordinal <= 2147483647:
                raise _error("MALFORMED_CALLBACK")
            return TelegramIntent(update.context, TelegramIntentKind.SELECT_SERVICE, MappingProxyType({"catalog_version": catalog, "option_ordinal": ordinal}), callback_id)
        if data.startswith("v1:"):
            raise _error("MALFORMED_CALLBACK")
        raise _error("UNSUPPORTED_CALLBACK")


map_telegram_interaction = TelegramInteractionMapper().map
