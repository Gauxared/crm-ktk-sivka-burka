"""Application runtime joining authenticated Telegram updates to the reviewed funnel."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from apps.api.sivka_burka_api.channel_identity import ChannelIdentityService, ChannelIdentityError
from .telegram_conversation import TelegramConversationOrchestrator
from .telegram_rendering import TelegramOutcomeRenderer
from .telegram_transport import TelegramBotApiClient, TelegramTransportError
from .telegram_updates import AcceptedTelegramUpdate, IgnoredTelegramUpdate, TelegramUpdateAdapter, TelegramUpdateError


class TelegramWebhookError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Invalid Telegram webhook")

    def __str__(self) -> str:
        return f"Telegram webhook error: {self.code}"


def _keyboard(buttons: tuple[Any, ...]) -> dict[str, object]:
    return {"inline_keyboard": [[{"text": button.label, "callback_data": button.callback_data}] for button in buttons]}


@dataclass(slots=True)
class TelegramWebhookRuntime:
    adapter: TelegramUpdateAdapter
    identities: ChannelIdentityService
    orchestrator: TelegramConversationOrchestrator
    renderer: TelegramOutcomeRenderer
    transport: TelegramBotApiClient

    async def handle(self, secret: str | None, payload: object) -> bool:
        try:
            update = self.adapter.parse(secret, payload)
        except TelegramUpdateError as error:
            raise TelegramWebhookError(error.code) from None
        if isinstance(update, IgnoredTelegramUpdate):
            return False
        if not isinstance(update, AcceptedTelegramUpdate):
            raise TelegramWebhookError("MALFORMED_UPDATE")
        try:
            self.identities.ensure(update.context)
            rendered = self.renderer.render(self.orchestrator.handle(update))
        except (ChannelIdentityError, ValueError):
            raise TelegramWebhookError("PROCESSING_UNAVAILABLE") from None
        # Acknowledge first: the user should never be left with a spinning callback
        # after the local business transaction has completed.
        if rendered.callback_query_id is not None:
            await self.transport.answer_callback_query(rendered.callback_query_id)
        await self.transport.send_message(update.context.conversation_id, rendered.message.text, _keyboard(rendered.message.buttons))
        return True
