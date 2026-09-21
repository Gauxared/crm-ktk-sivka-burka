"""Small, injected Telegram Bot API boundary with deliberately safe failures."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import httpx


class TelegramTransportError(RuntimeError):
    """A retryable provider failure which never serializes provider material."""

    def __init__(self, code: str, *, retry_after: int | None = None) -> None:
        self.code, self.retry_after = code, retry_after
        super().__init__("Telegram transport unavailable")

    def __str__(self) -> str:
        return f"Telegram transport error: {self.code}"

    def __repr__(self) -> str:
        return f"TelegramTransportError({self.code!r})"


def _token(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or value != value.strip() or "\x00" in value:
        raise ValueError("Invalid Telegram runtime token")
    return value


def _retry_after(value: object) -> int | None:
    if not isinstance(value, Mapping):
        return None
    parameters = value.get("parameters")
    retry = parameters.get("retry_after") if isinstance(parameters, Mapping) else None
    return retry if isinstance(retry, int) and not isinstance(retry, bool) and retry > 0 else None


@dataclass(slots=True)
class TelegramBotApiClient:
    """Only exposes the two bot calls used by the application runtime."""

    token: str
    client: httpx.AsyncClient

    def __post_init__(self) -> None:
        self.token = _token(self.token)
        if not isinstance(self.client, httpx.AsyncClient):
            raise TypeError("Telegram HTTP client must be injected")

    async def answer_callback_query(self, callback_query_id: str) -> None:
        if not isinstance(callback_query_id, str) or not callback_query_id:
            raise TelegramTransportError("INVALID_CALLBACK")
        await self._call("answerCallbackQuery", {"callback_query_id": callback_query_id})

    async def send_message(self, chat_id: str, text: str, reply_markup: Mapping[str, Any]) -> None:
        if not isinstance(chat_id, str) or not chat_id or not isinstance(text, str) or not 1 <= len(text) <= 4096:
            raise TelegramTransportError("INVALID_MESSAGE")
        if not isinstance(reply_markup, Mapping):
            raise TelegramTransportError("INVALID_MESSAGE")
        await self._call("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": dict(reply_markup)})

    async def _call(self, method: str, payload: Mapping[str, Any]) -> None:
        try:
            response = await self.client.post(
                f"https://api.telegram.org/bot{self.token}/{method}", json=dict(payload), timeout=15.0
            )
            body = response.json()
        except Exception:
            raise TelegramTransportError("PROVIDER_UNAVAILABLE") from None
        if not isinstance(body, Mapping):
            raise TelegramTransportError("MALFORMED_PROVIDER_RESPONSE") from None
        if response.status_code != 200 or body.get("ok") is not True:
            raise TelegramTransportError("RATE_LIMITED" if response.status_code == 429 else "PROVIDER_UNAVAILABLE", retry_after=_retry_after(body)) from None
