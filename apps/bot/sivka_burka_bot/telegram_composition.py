"""Composition of the reviewed Telegram webhook runtime."""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy import Engine

from apps.api.sivka_burka_api.channel_conversation import ChannelConversation
from apps.api.sivka_burka_api.channel_draft_persistence import PostgresChannelDraftPort
from apps.api.sivka_burka_api.channel_gateway import ChannelGateway
from apps.api.sivka_burka_api.channel_identity import ChannelIdentityService
from apps.api.sivka_burka_api.settings import TelegramRuntimeSettings
from .telegram_actions import TelegramActionResolver
from .telegram_conversation import TelegramConversationOrchestrator
from .telegram_interactions import TelegramInteractionMapper
from .telegram_rendering import TelegramOutcomeRenderer
from .telegram_runtime import TelegramWebhookRuntime
from .telegram_screen_actions import TelegramScreenActionResolver
from .telegram_transport import TelegramBotApiClient
from .telegram_updates import TelegramUpdateAdapter


@dataclass(frozen=True, slots=True)
class ComposedTelegramRuntime:
    runtime: TelegramWebhookRuntime
    client: httpx.AsyncClient
    owns_client: bool


def compose_telegram_webhook_runtime(
    settings: TelegramRuntimeSettings,
    engine: Engine,
    client: httpx.AsyncClient | None = None,
) -> ComposedTelegramRuntime:
    """Build the complete runtime without adding Telegram business rules."""
    if not isinstance(settings, TelegramRuntimeSettings) or not settings.enabled:
        raise ValueError("Telegram runtime is not enabled")
    if not isinstance(engine, Engine):
        raise TypeError("Telegram runtime requires an Engine")
    if not all((settings.integration_id, settings.webhook_secret, settings.bot_token, settings.channel_hmac_secret, settings.digest_key_version)):
        raise ValueError("Telegram runtime settings are incomplete")
    owned_client = client is None
    http_client = httpx.AsyncClient() if owned_client else client
    assert http_client is not None
    port = PostgresChannelDraftPort(engine, settings.channel_hmac_secret, digest_key_version=settings.digest_key_version)
    conversation = ChannelConversation(ChannelGateway(port))
    runtime = TelegramWebhookRuntime(
        TelegramUpdateAdapter(settings.integration_id, settings.webhook_secret),
        ChannelIdentityService(engine),
        TelegramConversationOrchestrator(
            TelegramInteractionMapper(), TelegramActionResolver(), TelegramScreenActionResolver(), conversation
        ),
        TelegramOutcomeRenderer(),
        TelegramBotApiClient(settings.bot_token, http_client),
    )
    return ComposedTelegramRuntime(runtime, http_client, owned_client)
