import asyncio

from fastapi.testclient import TestClient

from apps.api.sivka_burka_api.main import create_app
from apps.bot.sivka_burka_bot.telegram_runtime import TelegramWebhookError
from apps.bot.sivka_burka_bot.telegram_transport import TelegramTransportError


class Runtime:
    def __init__(self, error=None): self.error, self.calls = error, []
    async def handle(self, secret, payload):
        self.calls.append((secret, payload))
        if self.error: raise self.error
        return payload.get("message") is not None


def request(client, payload={"update_id": 1, "message": {}} , **headers):
    return client.post("/api/v1/integrations/telegram/webhook", json=payload, headers={"content-type": "application/json", **headers})


def test_route_requires_media_type_and_injects_only_secret_header():
    runtime = Runtime()
    client = TestClient(create_app(telegram_webhook_runtime=runtime))
    assert client.post("/api/v1/integrations/telegram/webhook", content=b"{}").status_code == 415
    response = request(client, {"update_id": 1, "message": {}}, **{"X-Telegram-Bot-Api-Secret-Token": "synthetic"})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert runtime.calls == [("synthetic", {"update_id": 1, "message": {}})]


def test_safe_errors_and_ignored_acknowledgement():
    client = TestClient(create_app(telegram_webhook_runtime=Runtime(TelegramWebhookError("UNAUTHORIZED"))))
    assert request(client).status_code == 401
    client = TestClient(create_app(telegram_webhook_runtime=Runtime(TelegramTransportError("PROVIDER_UNAVAILABLE"))))
    assert request(client).status_code == 503
    client = TestClient(create_app(telegram_webhook_runtime=Runtime()))
    assert request(client, {"update_id": 1, "poll": {}}).json() == {"data": {"accepted": False}}


def test_body_cap_is_before_json_processing():
    runtime = Runtime()
    client = TestClient(create_app(telegram_webhook_runtime=runtime))
    response = client.post("/api/v1/integrations/telegram/webhook", content=b"x" * (256 * 1024 + 1), headers={"content-type": "application/json"})
    assert response.status_code == 413
    assert runtime.calls == []
