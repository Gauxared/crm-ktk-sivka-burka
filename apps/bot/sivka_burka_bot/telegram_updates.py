"""Pure, authenticated normalization of Telegram webhook updates.

The transport layer owns authentication and Telegram-shape validation.  The
shared conversation core receives only the immutable, trusted result types
defined here and never sees a raw Telegram mapping.
"""
from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from apps.api.sivka_burka_api.channel_gateway import ChannelContext, Platform


class TelegramUpdateError(ValueError):
    """A safe adapter error with no payload or secret details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Unauthorized Telegram update" if code == "UNAUTHORIZED" else "Malformed Telegram update")


class InputKind(str, Enum):
    TEXT = "TEXT"
    CALLBACK = "CALLBACK"


@dataclass(frozen=True, slots=True)
class AcceptedTelegramUpdate:
    context: ChannelContext
    input_kind: InputKind
    text: str | None = None
    data: str | None = None
    callback_query_id: str | None = None


@dataclass(frozen=True, slots=True)
class IgnoredTelegramUpdate:
    reason: str


_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}\Z")
_UPDATE_MAX = 2_147_483_647
_CALLBACK_ID_MAX = 256


def _integer(value: Any, *, minimum: int | None = None, maximum: int | None = None) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and (minimum is None or value >= minimum)
        and (maximum is None or value <= maximum)
    )


def _mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _nonempty_text(value: Any, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        return None
    return normalized


class TelegramUpdateAdapter:
    """Authenticate and normalize Telegram updates without side effects."""

    def __init__(self, integration_id: str, webhook_secret: str) -> None:
        if not isinstance(integration_id, str) or not 0 < len(integration_id) <= 128 or integration_id.strip() != integration_id or "\x00" in integration_id:
            raise ValueError("Invalid integration id")
        if not isinstance(webhook_secret, str) or _SECRET_RE.fullmatch(webhook_secret) is None:
            raise ValueError("Invalid webhook secret")
        self._integration_id = integration_id
        self._webhook_secret = webhook_secret

    def parse(self, secret_header: Any, body: Any) -> AcceptedTelegramUpdate | IgnoredTelegramUpdate:
        if not isinstance(secret_header, str) or not hmac.compare_digest(secret_header, self._webhook_secret):
            raise TelegramUpdateError("UNAUTHORIZED")
        if not _mapping(body) or not _integer(body.get("update_id"), minimum=0, maximum=_UPDATE_MAX):
            raise TelegramUpdateError("MALFORMED_UPDATE")

        has_message = "message" in body
        has_callback = "callback_query" in body
        if has_message and has_callback:
            raise TelegramUpdateError("MALFORMED_UPDATE")
        update_id = body["update_id"]
        if has_message:
            return self._message(update_id, body["message"])
        if has_callback:
            return self._callback(update_id, body["callback_query"])
        return IgnoredTelegramUpdate("UNSUPPORTED_UPDATE")

    def _context(self, update_id: int, sender_id: int, chat_id: int) -> ChannelContext:
        return ChannelContext(
            Platform.TELEGRAM,
            self._integration_id,
            str(sender_id),
            str(chat_id),
            f"telegram-update:{update_id}",
        )

    @staticmethod
    def _user(value: Any) -> int | None:
        if not _mapping(value) or not _integer(value.get("id"), minimum=0):
            return None
        if value.get("is_bot") is not False:
            return None
        return value["id"]

    @staticmethod
    def _private_chat(value: Any) -> int | None:
        if not _mapping(value) or value.get("type") != "private" or not _integer(value.get("id")):
            return None
        return value["id"]

    def _message(self, update_id: int, message: Any) -> AcceptedTelegramUpdate | IgnoredTelegramUpdate:
        if not _mapping(message):
            raise TelegramUpdateError("MALFORMED_UPDATE")
        chat_value = message.get("chat")
        if _mapping(chat_value) and chat_value.get("type") in {"group", "supergroup", "channel"}:
            return IgnoredTelegramUpdate("NON_PRIVATE_MESSAGE")
        user_value = message.get("from")
        if _mapping(user_value) and user_value.get("is_bot") is True:
            return IgnoredTelegramUpdate("BOT_MESSAGE")
        sender = self._user(message.get("from"))
        chat = self._private_chat(message.get("chat"))
        text = message.get("text")
        if sender is None or chat is None:
            raise TelegramUpdateError("MALFORMED_UPDATE")
        if not _integer(message.get("message_id"), minimum=0):
            raise TelegramUpdateError("MALFORMED_UPDATE")
        if text is None:
            return IgnoredTelegramUpdate("UNSUPPORTED_MESSAGE")
        normalized = _nonempty_text(text, 4096)
        if normalized is None:
            raise TelegramUpdateError("MALFORMED_UPDATE")
        return AcceptedTelegramUpdate(self._context(update_id, sender, chat), InputKind.TEXT, text=normalized)

    def _callback(self, update_id: int, callback: Any) -> AcceptedTelegramUpdate | IgnoredTelegramUpdate:
        if not _mapping(callback):
            raise TelegramUpdateError("MALFORMED_UPDATE")
        callback_id = _nonempty_text(callback.get("id"), _CALLBACK_ID_MAX)
        sender = self._user(callback.get("from"))
        data = callback.get("data")
        message = callback.get("message")
        if callback_id is None or sender is None:
            if sender is None and _mapping(callback.get("from")) and callback["from"].get("is_bot") is True:
                return IgnoredTelegramUpdate("BOT_CALLBACK")
            raise TelegramUpdateError("MALFORMED_UPDATE")
        if not _mapping(message):
            if message is None:
                return IgnoredTelegramUpdate("INLINE_CALLBACK")
            raise TelegramUpdateError("MALFORMED_UPDATE")
        chat = self._private_chat(message.get("chat"))
        if chat is None:
            return IgnoredTelegramUpdate("NON_PRIVATE_CALLBACK")
        if not _integer(message.get("message_id"), minimum=0):
            raise TelegramUpdateError("MALFORMED_UPDATE")
        if not isinstance(data, str) or not data or len(data.encode("utf-8")) > 64:
            raise TelegramUpdateError("MALFORMED_UPDATE")
        return AcceptedTelegramUpdate(self._context(update_id, sender, chat), InputKind.CALLBACK, data=data, callback_query_id=callback_id)
