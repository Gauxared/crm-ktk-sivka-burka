"""Configured Telegram composition evidence on disposable PostgreSQL."""
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
from sqlalchemy.engine import Engine, make_url

from apps.api.sivka_burka_api.main import create_app


ROOT = Path(__file__).resolve().parents[2]
WEBHOOK_SECRET = "synthetic_webhook_secret"
TOKEN = "123456:synthetic_token"
HEADERS = {
    "content-type": "application/json",
    "X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET,
}


def _database_url():
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return make_url(line.removeprefix("DATABASE_URL="))
    raise RuntimeError("synthetic DATABASE_URL is absent")


@pytest.fixture()
def database(monkeypatch: pytest.MonkeyPatch) -> Engine:
    name = f"sivka_tg018_{uuid4().hex}"
    target = _database_url().set(database=name)
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    engine: Engine | None = None
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
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.payloads: list[dict[str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(method)
        self.payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.calls)}})


class TrackedClient(httpx.AsyncClient):
    def __init__(self, provider: Provider) -> None:
        super().__init__(transport=httpx.MockTransport(provider))
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        await super().aclose()


def test_configured_create_app_uses_real_postgres_composition_and_preserves_injected_ownership(
    database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_INTEGRATION_ID", "telegram-primary")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHANNEL_HMAC_SECRET", "synthetic-hmac")
    monkeypatch.setenv("TELEGRAM_DIGEST_KEY_VERSION", "1")
    provider = Provider()
    client = TrackedClient(provider)

    with TestClient(create_app(telegram_engine=database, telegram_http_client=client)) as test_client:
        response = test_client.post(
            "/api/v1/integrations/telegram/webhook",
            headers=HEADERS,
            json={
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "from": {"id": 42, "is_bot": False},
                    "chat": {"id": 42, "type": "private"},
                    "text": "/start",
                },
            },
        )

    assert response.status_code == 200
    assert response.json() == {"data": {"accepted": True}}
    assert provider.calls == ["sendMessage"]
    assert provider.payloads[0]["chat_id"] == "42"
    assert client.close_calls == 0
    with database.connect() as connection:
        identity = connection.execute(text("SELECT platform, integration_id, external_sender_id FROM channel_identities")).one()
    assert identity == ("TELEGRAM", "telegram-primary", "42")
    with database.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1
    database.dispose()
    asyncio.run(client.aclose())
    assert client.close_calls == 1
