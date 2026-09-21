import asyncio
import httpx
import pytest

from apps.bot.sivka_burka_bot.telegram_transport import TelegramBotApiClient, TelegramTransportError


def client(handler):
    return TelegramBotApiClient("synthetic-token", httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_fixed_url_and_only_reviewed_payload_are_sent():
    requests = []
    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": True})
    api = client(handler)
    asyncio.run(api.answer_callback_query("callback"))
    asyncio.run(api.send_message("42", "hello", {"inline_keyboard": []}))
    assert [item.url.host for item in requests] == ["api.telegram.org", "api.telegram.org"]
    assert [item.url.path.rsplit("/", 1)[-1] for item in requests] == ["answerCallbackQuery", "sendMessage"]
    asyncio.run(api.client.aclose())


@pytest.mark.parametrize("status, body, code", [
    (429, {"ok": False, "parameters": {"retry_after": 3}, "description": "private"}, "RATE_LIMITED"),
    (500, {"ok": False, "description": "private"}, "PROVIDER_UNAVAILABLE"),
    (200, {"ok": "yes", "description": "private"}, "PROVIDER_UNAVAILABLE"),
])
def test_provider_failures_are_safe(status, body, code):
    api = client(lambda request: httpx.Response(status, json=body))
    with pytest.raises(TelegramTransportError) as raised:
        asyncio.run(api.answer_callback_query("callback"))
    assert raised.value.code == code
    assert "synthetic-token" not in str(raised.value)
    assert "private" not in repr(raised.value)
    asyncio.run(api.client.aclose())
