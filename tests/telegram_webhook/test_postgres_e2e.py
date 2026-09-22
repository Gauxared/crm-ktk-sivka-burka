"""Disposable PostgreSQL evidence for the injected Telegram webhook slice."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from apps.api.sivka_burka_api.channel_conversation import ChannelConversation
from apps.api.sivka_burka_api.channel_draft_persistence import PostgresChannelDraftPort
from apps.api.sivka_burka_api.channel_gateway import ChannelGateway
from apps.api.sivka_burka_api.channel_identity import ChannelIdentityService
from apps.api.sivka_burka_api.main import create_app
from apps.bot.sivka_burka_bot.telegram_actions import TelegramActionResolver
from apps.bot.sivka_burka_bot.telegram_conversation import TelegramConversationOrchestrator
from apps.bot.sivka_burka_bot.telegram_interactions import TelegramInteractionMapper
from apps.bot.sivka_burka_bot.telegram_rendering import TelegramOutcomeRenderer
from apps.bot.sivka_burka_bot.telegram_runtime import TelegramWebhookRuntime
from apps.bot.sivka_burka_bot.telegram_screen_actions import TelegramScreenActionResolver
from apps.bot.sivka_burka_bot.telegram_transport import TelegramBotApiClient
from apps.bot.sivka_burka_bot.telegram_updates import TelegramUpdateAdapter


ROOT = Path(__file__).resolve().parents[2]
WEBHOOK_SECRET = "synthetic_webhook_secret"
TOKEN = "123456:synthetic_token"
HEADERS = {"content-type": "application/json", "X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}
DETAILS = "Участники: 2\nДата: 2026-10-01\nВремя: 10:00\nОпыт: новичок\nКомментарий: -"


def _database_url():
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return make_url(line.removeprefix("DATABASE_URL="))
    raise RuntimeError("synthetic DATABASE_URL is absent")


@pytest.fixture()
def database(monkeypatch):
    name = f"sivka_tg016_{uuid4().hex}"
    target = _database_url().set(database=name)
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    engine = None
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        monkeypatch.setenv("DATABASE_URL", target.render_as_string(hide_password=False))
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        engine = create_engine(target)
        with engine.begin() as connection:
            service_id, option_id = uuid4(), uuid4()
            connection.execute(text("INSERT INTO club_settings (id, timezone, location_link, visit_rules) VALUES (1, 'Asia/Bangkok', 'https://example.invalid/map', 'Synthetic rules')"))
            connection.execute(text("INSERT INTO services (id, code, title, description, information, active, sort_order) VALUES (:id, 'ride', 'Групповая прогулка', 'Synthetic', 'Synthetic', true, 1)"), {"id": service_id})
            connection.execute(text("INSERT INTO service_options (id, service_id, code, duration_minutes, pricing_mode, price_minor, currency, active) VALUES (:id, :service, 'ride-60', 60, 'FIXED_PER_PERSON', 350000, 'RUB', true)"), {"id": option_id, "service": service_id})
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        admin.dispose()
        cleanup = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
        with cleanup.begin() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        cleanup.dispose()


class Provider:
    def __init__(self):
        self.calls: list[str] = []
        self.payloads: list[dict] = []
        self.fail_next_send = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(method)
        self.payloads.append(json.loads(request.content))
        if method == "sendMessage" and self.fail_next_send:
            self.fail_next_send = False
            return httpx.Response(500, json={"ok": False, "description": "synthetic private"})
        result = True if method == "answerCallbackQuery" else {"message_id": len(self.calls)}
        return httpx.Response(200, json={"ok": True, "result": result})


def message(update_id: int, value: str) -> dict:
    return {"update_id": update_id, "message": {"message_id": update_id, "from": {"id": 42, "is_bot": False}, "chat": {"id": 42, "type": "private"}, "text": value}}


def callback(update_id: int, value: str) -> dict:
    return {"update_id": update_id, "callback_query": {"id": f"callback-{update_id}", "from": {"id": 42, "is_bot": False}, "message": {"message_id": update_id, "chat": {"id": 42, "type": "private"}}, "data": value}}


def counts(database) -> dict[str, int]:
    tables = ("contact_cards", "channel_identities", "conversation_drafts", "inquiries", "inquiry_terms", "operation_receipts", "receipt_inquiries", "channel_events", "change_events", "event_inquiries", "notification_jobs")
    with database.connect() as connection:
        return {name: connection.execute(text(f"SELECT count(*) FROM {name}")).scalar_one() for name in tables}


@pytest.fixture()
def webhook(database):
    provider = Provider()
    http = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    gateway = ChannelGateway(PostgresChannelDraftPort(database, b"tg016-persistence-secret"))
    runtime = TelegramWebhookRuntime(
        TelegramUpdateAdapter("integration", WEBHOOK_SECRET),
        ChannelIdentityService(database),
        TelegramConversationOrchestrator(TelegramInteractionMapper(), TelegramActionResolver(), TelegramScreenActionResolver(), ChannelConversation(gateway)),
        TelegramOutcomeRenderer(),
        TelegramBotApiClient(TOKEN, http),
    )
    yield TestClient(create_app(telegram_webhook_runtime=runtime)), provider
    asyncio.run(http.aclose())


def post(client: TestClient, payload: dict):
    return client.post("/api/v1/integrations/telegram/webhook", json=payload, headers=HEADERS)


def test_real_route_full_funnel_and_post_commit_replays(database, webhook):
    client, provider = webhook
    ignored = post(client, {"update_id": 0, "poll": {"id": "synthetic"}})
    assert ignored.status_code == 200 and ignored.json() == {"data": {"accepted": False}}
    assert counts(database)["channel_identities"] == 0 and provider.calls == []
    assert post(client, message(1, "/start")).status_code == 200
    assert counts(database)["channel_identities"] == 1

    provider.fail_next_send = True
    selection = callback(2, "v1:s:1:0")
    assert post(client, selection).status_code == 503
    after_failed_save = counts(database)
    assert (after_failed_save["conversation_drafts"], after_failed_save["operation_receipts"], after_failed_save["channel_events"]) == (1, 1, 1)
    assert post(client, selection).status_code == 200
    assert counts(database) == after_failed_save

    before_altered = counts(database)
    outbound_before = len(provider.calls)
    assert post(client, callback(2, "v1:h")).status_code == 503
    assert counts(database) == before_altered
    assert len(provider.calls) == outbound_before

    assert post(client, message(3, "Алиса")).status_code == 200
    assert post(client, message(4, DETAILS)).status_code == 200

    provider.fail_next_send = True
    submit = callback(5, "v1:u")
    assert post(client, submit).status_code == 503
    after_failed_submit = counts(database)
    assert post(client, submit).status_code == 200
    assert counts(database) == after_failed_submit

    assert after_failed_submit == {
        "contact_cards": 1, "channel_identities": 1, "conversation_drafts": 1,
        "inquiries": 1, "inquiry_terms": 1, "operation_receipts": 4,
        "receipt_inquiries": 1, "channel_events": 4, "change_events": 1,
        "event_inquiries": 1, "notification_jobs": 1,
    }
    with database.connect() as connection:
        draft = connection.execute(text("SELECT version, step, closed_at IS NOT NULL FROM conversation_drafts")).one()
        markers = connection.execute(text("SELECT result_json->>'interaction_marker' FROM operation_receipts ORDER BY accepted_at")).scalars().all()
        assert draft == (4, "REVIEW", True)
        assert all(isinstance(value, str) and len(value) == 64 for value in markers)
    # Every callback attempt acknowledges before it tries to send the message.
    callback_calls = [provider.calls[index:index + 2] for index, value in enumerate(provider.calls) if value == "answerCallbackQuery"]
    assert callback_calls and all(pair == ["answerCallbackQuery", "sendMessage"] for pair in callback_calls)
    sends = [payload for method, payload in zip(provider.calls, provider.payloads) if method == "sendMessage"]
    assert sends and all(payload["chat_id"] == "42" for payload in sends)
    assert all(set(payload) == {"chat_id", "text", "reply_markup"} for payload in sends)
    assert any(payload["reply_markup"]["inline_keyboard"] for payload in sends)
