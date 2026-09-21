"""Deterministic orchestration from trusted Telegram updates to conversation results."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from apps.api.sivka_burka_api.channel_conversation import (
    ChannelAction,
    ChannelConversation,
    ConversationResult,
)
from .telegram_actions import TelegramActionResolver
from .telegram_interactions import TelegramInteractionMapper, TelegramIntentKind
from .telegram_screen_actions import TelegramScreenActionResolver
from .telegram_updates import AcceptedTelegramUpdate


class TelegramConversationError(ValueError):
    """Safe orchestration error with a stable code and no caller data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Invalid Telegram conversation")

    def __str__(self) -> str:
        return f"Telegram conversation error: {self.code}"


@dataclass(frozen=True, slots=True)
class TelegramConversationOutcome:
    result: ConversationResult
    callback_query_id: str | None = None


def _empty_action() -> ChannelAction:
    return ChannelAction("OPEN", MappingProxyType({}))


class TelegramConversationOrchestrator:
    """Coordinate the reviewed mapper, resolver and shared conversation."""

    def __init__(
        self,
        mapper: TelegramInteractionMapper,
        resolver: TelegramActionResolver,
        screen_resolver: TelegramScreenActionResolver,
        conversation: ChannelConversation,
    ) -> None:
        if type(mapper) is not TelegramInteractionMapper:
            raise TypeError("Invalid Telegram interaction mapper")
        if type(resolver) is not TelegramActionResolver:
            raise TypeError("Invalid Telegram action resolver")
        if type(screen_resolver) is not TelegramScreenActionResolver:
            raise TypeError("Invalid Telegram screen action resolver")
        if type(conversation) is not ChannelConversation:
            raise TypeError("Invalid channel conversation")
        self._mapper = mapper
        self._resolver = resolver
        self._screen_resolver = screen_resolver
        self._conversation = conversation

    def handle(self, update: AcceptedTelegramUpdate) -> TelegramConversationOutcome:
        intent = self._mapper.map(update)
        context = intent.context
        if intent.kind is TelegramIntentKind.SELECT_SERVICE:
            preflight = self._conversation.handle(context, _empty_action())
            catalog = self._catalog(preflight)
            action = self._resolver.resolve(intent, catalog)
            result = self._conversation.handle(context, action)
            return TelegramConversationOutcome(result, intent.callback_query_id)

        if intent.kind is TelegramIntentKind.TEXT_INPUT:
            preflight = self._conversation.handle(context, _empty_action())
            action = self._screen_resolver.resolve(intent, preflight)
            result = self._conversation.handle(context, action)
            return TelegramConversationOutcome(result, intent.callback_query_id)

        action = self._resolver.resolve(intent, None)
        result = self._conversation.handle(context, action)
        return TelegramConversationOutcome(result, intent.callback_query_id)

    @staticmethod
    def _catalog(result: ConversationResult) -> Mapping[str, Any]:
        if type(result) is not ConversationResult or not isinstance(result.data, Mapping):
            raise TelegramConversationError("CATALOG_UNAVAILABLE")
        catalog = result.data.get("catalog")
        if not isinstance(catalog, Mapping):
            raise TelegramConversationError("CATALOG_UNAVAILABLE")
        return catalog


orchestrate_telegram_conversation = TelegramConversationOrchestrator
