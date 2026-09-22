import httpx
import pytest
from sqlalchemy import create_engine
from fastapi.testclient import TestClient

from apps.api.sivka_burka_api import main
from apps.api.sivka_burka_api.settings import Settings, TelegramRuntimeSettings
from apps.bot.sivka_burka_bot.telegram_composition import ComposedTelegramRuntime, compose_telegram_webhook_runtime


def configured_settings() -> TelegramRuntimeSettings:
    return TelegramRuntimeSettings(True, "telegram-primary", "synthetic_secret", "1234567890:synthetic_token", b"synthetic-hmac", 1)


def test_factory_builds_reviewed_chain_with_injected_client() -> None:
    engine = create_engine("sqlite://")
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True, "result": True})))
    composed = compose_telegram_webhook_runtime(configured_settings(), engine, client)
    assert composed.runtime.adapter.__class__.__name__ == "TelegramUpdateAdapter"
    assert composed.runtime.identities.engine is engine
    assert composed.runtime.transport.client is client
    assert composed.owns_client is False
    engine.dispose()


def test_factory_rejects_disabled_settings() -> None:
    with pytest.raises(ValueError, match="enabled"):
        compose_telegram_webhook_runtime(TelegramRuntimeSettings(False), create_engine("sqlite://"))


def test_explicit_runtime_takes_precedence_over_enabled_composition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_INTEGRATION_ID", "telegram-primary")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "synthetic_secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1234567890:synthetic_token")
    monkeypatch.setenv("TELEGRAM_CHANNEL_HMAC_SECRET", "synthetic-hmac")
    monkeypatch.setenv("TELEGRAM_DIGEST_KEY_VERSION", "1")

    class Runtime:
        async def handle(self, secret: str | None, payload: object) -> bool:
            return secret == "synthetic_secret" and payload == {"update_id": 1}

    response = TestClient(main.create_app(telegram_webhook_runtime=Runtime())).post(
        "/api/v1/integrations/telegram/webhook",
        headers={"Content-Type": "application/json", "X-Telegram-Bot-Api-Secret-Token": "synthetic_secret"},
        json={"update_id": 1},
    )
    assert response.status_code == 200
    assert response.json() == {"data": {"accepted": True}}


def test_app_closes_only_its_composed_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    class Runtime:
        async def handle(self, secret: str | None, payload: object) -> bool:
            return False

    class Client:
        closed = 0

        async def aclose(self) -> None:
            self.closed += 1

    class Engine:
        disposed = 0

        def dispose(self) -> None:
            self.disposed += 1

    client, engine = Client(), Engine()
    settings = Settings("test", None, None, None, configured_settings())
    monkeypatch.setattr(main, "load_settings", lambda: settings)
    monkeypatch.setattr(main, "create_engine", lambda url: engine)
    monkeypatch.setattr(main, "database_url_from_environment", lambda: "synthetic://")
    monkeypatch.setattr(main, "compose_telegram_webhook_runtime", lambda settings, engine: ComposedTelegramRuntime(Runtime(), client, True))
    with TestClient(main.create_app()):
        pass
    assert client.closed == 1
    assert engine.disposed == 1
